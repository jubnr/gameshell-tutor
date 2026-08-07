#!/usr/bin/env python3
# tutor_daemon.py — background engine for the in-game "Maître du Jeu". GPLv3.
#
# Spawned by the shim at session start (frontend "in_game", the default).
# Same pipeline as tutor_pane.py (SessionBridge → TutorEngine → LLM) but the
# delivery is a spool: each utterance is rendered (color, character prefix,
# indentation) into ONE file under $SESSION/outbox/, written tmp-then-rename
# so the shim only ever sees complete files. The shim prints them at prompt
# time and `gm` waits for files named *-reply-<id>.msg.
#
# Learner → daemon: the shim's gm() appends {"event":"chat","id":N,"msg":...}
# lines to $SESSION/chat.jsonl; we tail it like bridge tails turns.jsonl.
#
# Lifecycle: exits when the game shell dies or the extracted game dir is
# removed (GameShell deletes it on exit); saves the learner model on the way
# out and removes daemon.pid.

import glob
import hashlib
import json
import os
import signal
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge import SessionBridge                      # noqa: E402
from engine import TutorEngine                        # noqa: E402
from learner_model import LearnerModel                # noqa: E402
from llm import (make_client, MockLLMClient, PERSONAS, T,  # noqa: E402
                 GM_COMMAND_MAP, DROPPED_COMMAND_ENTRIES,
                 GOAL_SENTENCE_REWRITES, GOAL_SENTENCE_REWRITES_AUTO,
                 NARRATION_FORMAT, strip_gm_echo)
from tutor_pane import load_config, wait_for_session, TUTOR_HOME, IDLE_SECONDS  # noqa: E402
try:
    from rag import Retriever
except Exception:                                     # numpy missing, etc.
    Retriever = None

PREFIX = {"fr": "\033[1;35m🧙 Le Maître du Jeu —\033[0m",
          "en": "\033[1;35m🧙 The Game Master —\033[0m"}
ABOUT = {"fr": "à propos de", "en": "about"}

GM_ART = r"""
        .
       /=\\       _____
      /===\\     |  *  |
     /=====\\    | * * |
    /=======\\   |_____|
      |   |    le Maître du Jeu
      |   |
"""

# Chapter art, one piece per mission FAMILY (the part before the "/" in a
# mission name), drawn above the briefing so each chapter of the castle has
# its own face. Plain ASCII on purpose: it must survive any terminal font and
# the magenta colouring the outbox applies line by line. Every line stays
# under 44 columns, because the outbox indents by 3 and the game is played in
# ordinary 80-column terminals. Set GSH_TUTOR_ART=0 to turn it all off.
MISSION_ART = {
    # the keep itself: cd, ls, mkdir, cp, mv, rm
    "basic": r"""
       |>>>            |>>>
    ___|________________|___
   |_|_|_|_|_|_|_|_|_|_|_|_|
   |   ___     ___     ___  |
   |  |   |   | + |   |   | |
   |__|___|___|___|___|___|_|""",
    # aliases, tab completion, jobs: the workshop
    "intermediate": r"""
      \    .-'''-.    /
       \  /  o o  \  /
        >(    ^    )<
       /  \  '-'  /  \
      /    '-...-'    \
""",
    # the maze: find, tree, xargs
    "finding_files_maze": r"""
   #########################
   #     #     #     #     #
   #  #  #  #  #  #  #  #  #
   #  #     #     #     #  #
   #  ###################  #
   #           >>>         #
   #########################""",
    # the book of potions: head, tail, cat, pipes
    "pipe_intro_book_of_potions": r"""
        .-.        _______
       /   \      /       \
      |  ~  |    | ~~~~~~~ |
      |  ~  |    | ~~~~~~~ |
       \___/     |_________|
      __|_|__     potions""",
    # the merchant's stall: long ledgers, pipelines
    "pipes_merchant_stall": r"""
    /\/\/\/\/\/\/\/\/\/\/\
   |                      |
   |   [$]   [#]   [%]    |
   |______________________|
        ||          ||""",
    # ps, kill, pstree: the spirits of the castle
    "processes": r"""
      .-.      .-.      .-.
     ( o )    ( o )    ( o )
      )_(      )_(      )_(
     /   \    /   \    /   \
      pid      pid      pid""",
    # Merlin and the three streams
    "stdin_stdout_stderr": r"""
    stdin  ===\
                >===[ ? ]===> stdout
    stderr ===/
                \===========> 2>&1""",
    # chmod: the locked quarters
    "permissions": r"""
          _______
         /       \
        |  .---.  |
        |  | o |  |
        |__|___|__|
           r w x""",
    # cal, nano, tr: the observatory and the scriptorium
    "misc": r"""
        *      .        *
     .      \   |   /       .
        *  --  -*-  --   *
     .      /   |   \       .
        *      .        *""",
    # the end of the road
    "FINAL_MISSION": r"""
      \\     |     //
       .-'''-.-'''-.
      (  *    *    * )
       '-.._____..-'
        |_________|
         victoire !""",
}


# How long the shell should trust a `pending` marker before deciding the
# daemon is wedged. Refreshed on every iteration that still has work, so a
# slow LLM call keeps extending its own lease.
PENDING_TTL = 15


def mark_pending(path):
    """Say "an answer is on its way" to the shell's prompt hook.

    Carries the pid and a deadline: without them a daemon killed mid-iteration
    left this file behind forever, and the shim then held EVERY prompt for its
    full 12s cap, silently, for the rest of the session. Written tmp-then-
    rename so the shim never reads a half-written line."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write("%d %d\n" % (os.getpid(), int(time.time()) + PENDING_TTL))
        os.rename(tmp, path)
    except OSError:
        pass


CURSOR_VERSION = 1


def load_cursor(session_dir):
    """Where the previous daemon for this session had read up to.

    `gm` respawns a dead daemon, and without this the new one re-read all of
    turns.jsonl and chat.jsonl and re-emitted every briefing, every error
    diagnosis and every chat answer of the session onto the next prompt."""
    try:
        with open(os.path.join(session_dir, "cursor.json")) as f:
            c = json.load(f)
        if c.get("v") != CURSOR_VERSION:
            return 0, 0
        return int(c.get("turns", 0)), int(c.get("chat", 0))
    except (OSError, ValueError, TypeError):
        return 0, 0


def save_cursor(session_dir, turns, chat):
    tmp = os.path.join(session_dir, "cursor.json.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump({"v": CURSOR_VERSION, "turns": turns, "chat": chat}, f)
        os.rename(tmp, os.path.join(session_dir, "cursor.json"))
    except OSError:
        pass


class Journal:
    """One JSON line per thing the Game Master says, in $SESSION/tutor.jsonl.

    Without it a session records only the learner's half: what the tutor said
    survived as ANSI-coloured text inside the typescript, interleaved with
    game output, with no kind, no hint level, no backend and no link to the
    turn that provoked it. That makes "did the tutor help?" unanswerable from
    a session directory, and it makes hint_level unrecoverable as a time
    series (learner_model.json only ever holds its final value).
    """

    def __init__(self, session_dir):
        self.path = os.path.join(session_dir, "tutor.jsonl")

    def write(self, rec):
        rec.setdefault("ts", int(time.time()))
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def drain(self, engine):
        """Everything engine._say produced since the last drain."""
        for rec in engine.spoken:
            self.write(rec)
        engine.spoken = []


def write_condition(session_dir, cfg, lang, rag_on, tutor_root):
    """What this session was actually run with.

    session.json describes the game; nothing described the tutor. config.json
    lives outside the session and can change between runs, so after the fact
    there was no way to tell a mock session from an LLM one — which is the
    first thing any comparison needs."""
    rec = {"llm": cfg.get("llm"), "persona": cfg.get("persona"), "lang": lang,
           "rag": bool(rag_on), "art": os.environ.get("GSH_TUTOR_ART") != "0",
           "model": os.environ.get("GSH_TUTOR_LLM_MODEL", ""),
           "started": int(time.time())}
    try:
        import subprocess
        rec["tutor_rev"] = subprocess.run(
            ["git", "-C", tutor_root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        rec["tutor_rev"] = ""
    try:
        with open(os.path.join(session_dir, "condition.json"), "w") as f:
            json.dump(rec, f, indent=1)
    except OSError:
        pass


def load_no_probe():
    """Missions the shell cannot probe for success (see scan_unsafe_checks in
    install.sh). The engine tells the learner to submit those with `gm fini`,
    and the briefing renderer keeps the check sentences instead of promising
    an automatic victory that will never come."""
    names = []
    try:
        with open(os.path.join(TUTOR_HOME, "no-probe.list")) as f:
            names = [l.strip() for l in f if l.strip()]
    except OSError:
        pass
    return names


# Delivery directive, invisible like the \x06 page break: the shim prints a
# marked line with no typewriter delay. Pacing is there to make the Game
# Master sound like someone speaking; ASCII art is scenery, and dripping it
# out a line at a time just looks like a slow terminal.
INSTANT = "\x0e"


def instant(block):
    """Mark every line of a block for immediate, unpaced printing."""
    if not block:
        return block
    return "\n".join(INSTANT + l for l in block.split("\n"))


def mission_art(mission_name):
    """Art for a mission's family, or "" (unknown family, or art disabled)."""
    if os.environ.get("GSH_TUTOR_ART") == "0":
        return ""
    family = (mission_name or "").split("/")[0]
    return MISSION_ART.get(family, "").strip("\n")


# one-liner ambience, spoken the FIRST time the adventurer enters a place
AMBIANCE = {
    "Cave": {"fr": "Tu descends dans la Cave… l'air y est frais, et quelque chose bruisse dans l'ombre.",
             "en": "You climb down into the Cellar… the air is cool, and something rustles in the dark."},
    "Donjon": {"fr": "Le Donjon se dresse devant toi. Ses étages s'empilent vers le ciel.",
               "en": "The Keep towers before you, floor upon floor reaching for the sky."},
    "Haut_du_donjon": {"fr": "Du haut du donjon, tout le domaine s'étend sous tes yeux.",
                       "en": "From the top of the tower, the whole realm stretches below."},
    "Salle_du_trone": {"fr": "La Salle du trône. Les tapisseries murmurent l'histoire du royaume.",
                       "en": "The Throne room. The tapestries whisper the kingdom's history."},
    "Bibliotheque": {"fr": "La Bibliothèque… des grimoires à perte de vue, et l'odeur du vieux papier.",
                     "en": "The Library… grimoires as far as the eye can see, and the smell of old paper."},
    "Grande_salle": {"fr": "La Grande salle résonne de tes pas.",
                     "en": "The Great hall echoes with your footsteps."},
    "Observatoire": {"fr": "L'Observatoire. D'ici, on lit les étoiles — et parfois l'avenir.",
                     "en": "The Observatory. From here one reads the stars — and sometimes the future."},
    "Foret": {"fr": "La Forêt t'enveloppe. Reste sur le sentier, aventurier·ère.",
              "en": "The Forest closes around you. Stay on the path, adventurer."},
    "Jardin": {"fr": "Le Jardin. Tout semble paisible… en apparence.",
               "en": "The Garden. Everything seems peaceful… on the surface."},
    "Labyrinthe": {"fr": "Le Labyrinthe ! Beaucoup y sont entrés, peu savent en sortir. `pwd` sera ta boussole.",
                   "en": "The Maze! Many enter, few find their way out. `pwd` will be your compass."},
    "Echoppe": {"fr": "L'Échoppe du marchand. Ses registres sont réputés… interminables.",
                "en": "The merchant's Stall. His ledgers are famously… endless."},
    "Montagne": {"fr": "La Montagne. Le vent siffle entre les pierres.",
                 "en": "The Mountain. Wind whistles between the stones."},
    "Grotte": {"fr": "La Grotte. Ta propre voix te revient en écho.",
               "en": "The Cave. Your own voice echoes back at you."},
}


class Outbox:
    def __init__(self, session_dir):
        self.dir = os.path.join(session_dir, "outbox")
        os.makedirs(self.dir, exist_ok=True)
        # Resume the numbering from what is already spooled. Filenames are
        # the delivery order (the shim prints them lexicographically), so a
        # respawned daemon restarting at 000001 would queue its messages
        # BEFORE anything still undelivered, and could collide with it.
        self.n = 0
        try:
            for name in os.listdir(self.dir):
                head = name[:6]
                if head.isdigit():
                    self.n = max(self.n, int(head))
        except OSError:
            pass

    def janitor(self):
        # the shell can't always unlink delivered files (the game wraps rm);
        # it truncates them instead — sweep empty files older than 5s
        now = time.time()
        try:
            for name in os.listdir(self.dir):
                p = os.path.join(self.dir, name)
                st = os.stat(p)
                if st.st_size == 0 and now - st.st_mtime > 5:
                    os.remove(p)
        except OSError:
            pass

    def post(self, text, lang="en", ref_cmd=None, reply_id=None,
             continuation=False):
        self.janitor()
        if not text:
            return
        self.n += 1
        name = "%06d%s.msg" % (self.n,
                               "-reply-%s" % reply_id if reply_id else "")
        if continuation:
            # follow-up chunk of a message being streamed: no name tag
            head = ""
        else:
            head = PREFIX.get(lang, PREFIX["en"])
            if ref_cmd:
                head += " \033[2m(%s `%s`)\033[0m" % (
                    ABOUT.get(lang, ABOUT["en"]), ref_cmd[:60])
            head += "\n"
        # whole message in magenta so the GM is recognizable at a glance;
        # blank lines stay truly blank (the shim paces paragraphs on them)
        body = head + "\n".join(
            "   \033[35m" + l + "\033[0m" if l.strip() else ""
            for l in text.splitlines())
        tmp = os.path.join(self.dir, ".tmp-%s" % name)
        with open(tmp, "w") as f:
            f.write(body + "\n")
        os.rename(tmp, os.path.join(self.dir, name))

    def post_marker(self, reply_id):
        """Empty completion marker: unblocks gm() when the reply was already
        delivered as streamed chunks. Empty files are skipped by the shim's
        printer and swept by the janitor."""
        self.n += 1
        name = "%06d-reply-%s.msg" % (self.n, reply_id)
        tmp = os.path.join(self.dir, ".tmp-%s" % name)
        open(tmp, "w").close()
        os.rename(tmp, os.path.join(self.dir, name))


class StreamSink:
    """Turns LLM deltas into sentence/line-sized continuation messages so
    the Game Master speaks WHILE generating. Remembers completed texts so
    the daemon does not re-post what was already streamed."""

    MIN_CHUNK = 60  # chars before we look for a break point

    def __init__(self, outbox, lang):
        self.outbox = outbox
        self.lang = lang
        self.buf = ""
        self.first = True
        self.streamed = set()

    def feed(self, delta):
        self.buf += delta
        while True:
            cut = self.buf.find("\n")
            if cut < 0 and len(self.buf) > self.MIN_CHUNK:
                for sep in (". ", "! ", "? ", "; "):
                    p = self.buf.rfind(sep, self.MIN_CHUNK // 2)
                    if p > 0:
                        cut = p + 1
                        break
            if cut < 0:
                return
            chunk, self.buf = self.buf[:cut], self.buf[cut + 1:] \
                if self.buf[cut:cut + 1] == "\n" else self.buf[cut:]
            self._emit(chunk)

    def _emit(self, chunk):
        # a lone `gm` arrives as its own chunk and would reach the screen
        # before respond() can filter the finished reply — drop it here too
        chunk = strip_gm_echo(chunk)
        if chunk.strip():
            self.outbox.post(chunk.rstrip(), self.lang,
                             continuation=not self.first)
            self.first = False

    def close(self, full_text):
        # full_text None = the stream died mid-generation: drop the dangling
        # fragment rather than speaking half a sentence, since the caller is
        # about to post the mock fallback in its place
        if full_text is not None:
            self._emit(self.buf)
        self.buf = ""
        self.first = True
        if full_text:
            self.streamed.add(full_text.strip())

    def take(self, text):
        """True (and consume) if this exact text was already streamed."""
        key = (text or "").strip()
        if key in self.streamed:
            self.streamed.discard(key)
            return True
        return False


class ChatTail:
    def __init__(self, session_dir, offset=0):
        self.path = os.path.join(session_dir, "chat.jsonl")
        try:
            if offset > os.path.getsize(self.path):
                offset = 0
        except OSError:
            offset = 0
        self._offset = offset

    def poll(self):
        msgs = []
        if not os.path.exists(self.path):
            return msgs
        with open(self.path) as f:
            f.seek(self._offset)
            new = f.read()
            self._offset = f.tell()
        for line in new.splitlines():
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return msgs


class FastVerdictClient:
    """Route verdict/state messages to the instant deterministic templates;
    only conversational kinds (chat, error diagnosis) reach the configured
    LLM. Keeps mission transitions fluid: victory and
    next-mission briefing must land in the prompt's short delivery window,
    never behind a multi-second LLM call. (Tradeoff: the postmortem persona's
    check_pass critique is template-based when this routing is active.)"""

    FAST_KINDS = {"mission_start", "check_pass", "check_fail", "danger",
                  "hint_capped", "idle", "interactive_check"}

    def __init__(self, slow):
        self.slow = slow
        self.fast = MockLLMClient()
        # which client answered last — the journal records it, because the
        # configured backend is NOT what most utterances actually used
        self.last_route = "mock"

    def respond(self, ctx):
        self.last_route = "mock"
        if ctx.get("kind") in self.FAST_KINDS:
            return self.fast.respond(ctx)
        if (ctx.get("kind") == "chat"
                and ctx.get("message") == "hint request"):
            # gm indice: the graded per-mission hints are curated content —
            # deterministic, instant, never LLM-improvised
            return self.fast.respond(ctx)
        self.last_route = getattr(self.slow, "name", "slow")
        return self.slow.respond(ctx)


def game_alive(session):
    pid = session.get("pid")
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    root = session.get("gsh_root", "")
    return bool(root) and os.path.isdir(root)


def main():
    session_dir = sys.argv[1]
    with open(os.path.join(session_dir, "daemon.pid"), "w") as f:
        f.write(str(os.getpid()))
    try:
        run(session_dir)
    finally:
        # `pending` first: the shell must never see a live daemon.pid next to
        # a marker nobody will ever clear -- that combination made every
        # prompt wait for the full hold-back cap.
        for name in ("pending", "daemon.pid"):
            try:
                os.remove(os.path.join(session_dir, name))
            except OSError:
                pass


def run(session_dir):
    cfg = load_config()
    wait_for_session(session_dir)
    cur_turns, cur_chat = load_cursor(session_dir)
    bridge = SessionBridge(session_dir, offset=cur_turns)
    sess = bridge.session
    lang = (sess.get("lang") or "en")[:2]
    tutor_root = os.path.dirname(os.path.abspath(__file__))
    learner = LearnerModel(sess.get("gsh_config"), TUTOR_HOME, sess.get("uid"))
    outbox_early = Outbox(session_dir)
    slow_client = make_client(cfg["llm"])
    sink = StreamSink(outbox_early, lang)
    if hasattr(slow_client, "chunk_sink"):
        slow_client.chunk_sink = sink   # stream LLM replies sentence by sentence
        if getattr(slow_client, "url", ""):
            # pre-load the model while the learner reads the briefing —
            # otherwise the FIRST gm question pays the cold load (>18s)
            def warmup(url=slow_client.url, model=slow_client.model):
                try:
                    req = urllib.request.Request(
                        url, data=json.dumps({
                            "model": model, "max_tokens": 1, "stream": False,
                            "messages": [{"role": "user", "content": "ok"}],
                        }).encode(),
                        headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=120).read()
                    print("warmup done", flush=True)
                except Exception as exc:
                    print("warmup failed: %s" % exc, flush=True)
            threading.Thread(target=warmup, daemon=True).start()
    engine = TutorEngine(
        bridge, learner, FastVerdictClient(slow_client),
        meta_dir=os.path.join(tutor_root, "missions_meta"),
        goals_cache=os.path.join(TUTOR_HOME, "goals-cache"),
        lang=lang, persona=cfg["persona"],
        no_probe=load_no_probe())
    outbox = outbox_early
    journal = Journal(session_dir)
    chat = ChatTail(session_dir, offset=cur_chat)
    if cur_turns or cur_chat:
        # resuming a session a previous daemon was already handling: adopt
        # the mission it had reached, or the first live turn would look like
        # a mission change and re-brief a mission already told.
        starts = [nb for nb, act in bridge.mission_log() if act == "START"]
        if starts:
            engine.current_mission = starts[-1]
    retriever = Retriever() if Retriever else None
    use_rag = bool(retriever and retriever.ok and cfg["llm"] != "mock")
    print("rag index: %s" % ("ready" if use_rag else "off"), flush=True)
    write_condition(session_dir, cfg, lang, use_rag, tutor_root)

    def recall(query):
        """Ground the LLM with local man pages / mission texts; never fatal."""
        engine.knowledge = retriever.top(query) if use_rag else None

    def push():
        """Nudge the game shell (SIGUSR1) so a queued message is displayed
        while the learner idles at the prompt — ONLY when no command is in
        flight (an unmatched 'pre' means the shell is busy; signalling then
        could abort a mission check's `read`). The shim's handler re-checks
        at-prompt state; delivery is idempotent either way."""
        if bridge._open_turns:
            print("push suppressed: turn in flight %s"
                  % list(bridge._open_turns), flush=True)
            return
        try:
            os.kill(int(sess.get("pid")), signal.SIGUSR1)
            print("push sent to pid %s" % sess.get("pid"), flush=True)
        except (OSError, TypeError, ValueError) as exc:
            print("push failed: %s" % exc, flush=True)

    briefing_renderer = MockLLMClient()

    # A cached narration is RENDERED text, so it outlives the wording that
    # produced it: after `gm prediction` was removed the cache happily kept
    # advertising it at every mission start. Fingerprint the templates the
    # renderer actually uses and put that in the cache key, so editing any of
    # them silently retires every file rendered from the old wording.
    narration_key = hashlib.sha1(json.dumps(
        [[T.get(lang, T["en"]).get(k, "")
          for k in ("brief_intro", "brief_outro", "greet", "greet_generic")],
         GM_COMMAND_MAP, DROPPED_COMMAND_ENTRIES, MISSION_ART,
         GOAL_SENTENCE_REWRITES, GOAL_SENTENCE_REWRITES_AUTO,
         NARRATION_FORMAT,
         # mission_art() is silenced by GSH_TUTOR_ART=0, which the renderer
         # honours but the key did not: an artless briefing was cached once
         # and then served to every art-enabled session afterwards.
         os.environ.get("GSH_TUTOR_ART") == "0"],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:8]
    for stale in glob.glob(os.path.join(TUTOR_HOME, "goals-cache",
                                        "narration-*.txt")):
        if ".%s.txt" % narration_key not in stale:
            try:
                os.remove(stale)
            except OSError:
                pass

    def narration(mission_nb, quick=False):
        """The GM's telling of a mission (mission start + `gsh goal`).
        ALWAYS rendered by the deterministic mock template, never the LLM:
        a briefing must carry the goal's operational details verbatim, and
        small local models garble them. Cached per mission+lang+template
        fingerprint. quick=True (re-reads via gm mission): drop the page-break
        pauses when the mission was already told once (cache hit)."""
        name = bridge.mission_name(mission_nb) or str(mission_nb)
        cache = os.path.join(TUTOR_HOME, "goals-cache",
                             "narration-%s.%s.%s.txt" % (name.replace("/", "__"),
                                                         lang, narration_key))
        try:
            with open(cache) as f:
                text = f.read()
            if quick:
                text = text.replace("\x06\n", "").replace("\x06", "")
            return text
        except OSError:
            pass
        saved = engine.current_mission
        engine.current_mission = mission_nb
        try:
            text = briefing_renderer.respond(
                engine.build_context("mission_start"))
        finally:
            engine.current_mission = saved
        # the chapter's art heads the briefing, before the page-break pauses
        art = mission_art(name)
        if text and art:
            text = instant(art) + "\n\n" + text
        if text:
            try:
                os.makedirs(os.path.dirname(cache), exist_ok=True)
                with open(cache, "w") as f:
                    f.write(text)
            except OSError:
                pass
        return text

    last_activity = time.time()
    idle_nudged = False
    pending_path = os.path.join(session_dir, "pending")
    # A predecessor killed mid-iteration leaves this behind; clear it before
    # the shell can see a live daemon.pid next to a dead daemon's marker.
    try:
        os.remove(pending_path)
    except OSError:
        pass
    while game_alive(sess):
        posted_before = outbox.n
        turns = bridge.poll()
        chats = chat.poll()
        if turns or chats:
            # tell the shell "an answer is being prepared": its prompt hook
            # waits on this marker instead of releasing the prompt too early
            mark_pending(pending_path)
        for kind, payload in turns:
            last_activity, idle_nudged = time.time(), False
            if kind == "session_start":
                # the GM shows himself, then briefs the current mission
                # right at launch, before the learner types anything
                nb = str(payload.get("mission") or "")
                if nb and nb != engine.current_mission:
                    engine.current_mission = nb
                    engine.mission_commands = []
                    outbox.post(instant(GM_ART.strip("\n")) + "\n\n"
                                + (narration(nb) or ""), lang)
                    journal.write({"kind": "briefing", "mission_nb": nb,
                                   "backend": "template"})
                continue
            if kind != "turn":
                continue
            # mission change: brief through the narration cache (same text
            # as `gsh goal`), and pre-set the engine so it skips its own
            # uncached greeting
            if payload.mission != engine.current_mission:
                engine.current_mission = payload.mission
                engine.mission_commands = []
                outbox.post(narration(payload.mission) or "", lang)
                journal.write({"kind": "briefing",
                               "mission_nb": payload.mission,
                               "backend": "template"})
            # first discovery of a place: one line of ambience
            place = os.path.basename(payload.cwd or "")
            if place in AMBIANCE:
                visited = learner.data.setdefault("visited", [])
                if place not in visited:
                    visited.append(place)
                    learner.save()
                    outbox.post("✨ " + AMBIANCE[place].get(
                        lang, AMBIANCE[place]["en"]), lang)
                    journal.write({"kind": "ambience", "place": place,
                                   "mission_nb": payload.mission,
                                   "backend": "template"})

            is_check = (payload.cmd or "").startswith("gsh check")
            # attribute delayed coaching to the command it concerns — but
            # not for checks: verdict messages speak for themselves, and the
            # learner never typed "gsh check" (auto-detection did)
            ref = payload.cmd if (payload.exit != 0 and not is_check) else None
            if ref:
                recall("%s : %s" % (payload.cmd,
                                    (payload.output or "").splitlines()[0]
                                    if payload.output else ""))
            spoken = engine.on_turn(payload)
            journal.drain(engine)
            for utterance in spoken:
                if sink.take(utterance):
                    continue   # already on screen, streamed live
                outbox.post(utterance, lang, ref_cmd=ref)
            if is_check:
                # a passed check advances the game at once: brief the new
                # mission right away, in the same delivery window as the
                # victory message
                starts = [nb for nb, act in bridge.mission_log()
                          if act == "START"]
                if starts and starts[-1] != engine.current_mission:
                    engine.current_mission = starts[-1]
                    engine.mission_commands = []
                    outbox.post(narration(starts[-1]) or "", lang)
                    journal.write({"kind": "briefing",
                                   "mission_nb": starts[-1],
                                   "backend": "template"})

        for ev in chats:
            last_activity, idle_nudged = time.time(), False
            if ev.get("event") == "goal":
                nb = str(ev.get("mission") or engine.current_mission or "")
                journal.write({"kind": "goal_reread", "mission_nb": nb,
                               "backend": "template"})
                outbox.post(narration(nb, quick=True) or "…", lang,
                            reply_id=ev.get("id"))
                continue
            if ev.get("event") != "chat":
                continue
            msg = ev.get("msg", "")
            if msg.startswith("/persona"):
                name = msg.split(None, 1)[1].strip() if " " in msg else ""
                if name in PERSONAS:
                    engine.persona = name
                    reply = "persona → " + name
                else:
                    reply = "personas : " + ", ".join(PERSONAS)
            else:
                recall(msg)
                reply = engine.on_chat(msg)
                journal.drain(engine)
            if reply and sink.take(reply):
                outbox.post_marker(ev.get("id"))   # streamed live: just unblock gm
            else:
                outbox.post(reply or "…", lang, reply_id=ev.get("id"))

        if (time.time() - last_activity > IDLE_SECONDS and not idle_nudged
                and engine.current_mission):
            idle_nudged = True
            outbox.post(engine.on_idle() or "", lang)
            journal.drain(engine)

        engine.knowledge = None
        if turns or chats:
            try:
                os.remove(pending_path)
            except OSError:
                pass
            # everything read this iteration has now been answered: a daemon
            # respawned after this point resumes here instead of replaying
            save_cursor(session_dir, bridge._offset, chat._offset)
        if outbox.n != posted_before:
            push()
        time.sleep(0.4)

    learner.save()
    # the game tree is about to be deleted (or already is): keep the
    # progression files so the session can still be read afterwards
    bridge.snapshot_progress()
    # the probes' TMPDIR (see _tutor_probe): checks leave mktemp files there,
    # and it exists only to keep them OUT of the game tree
    try:
        import shutil
        shutil.rmtree(os.path.join(session_dir, "probe-tmp"), ignore_errors=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
