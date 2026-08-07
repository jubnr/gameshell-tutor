# engine.py — TutorEngine: per-turn context building and tutoring policy. GPLv3.
#
# Policy lives here (when to speak, hint ladder, stuck detection, learner
# model updates); wording lives in the LLM client (mock or HTTP).
# The engine never runs commands and never decides mission success —
# it only reads what the shell already did.

import json
import os
import re
import time

# Commands worth a word of caution AFTER the shell has run them (the tutor
# never intercepts: the shell acts, we comment).
#
# The old pattern required `rm -r`/`rm -rf` before `/ ~ * ..`, which matches
# almost nothing this game teaches. The missions here are about `rm
# *_spider_*` and `rm .*_spider_*` in a cellar that also holds two signed bat
# files, and the catastrophic beginner move is a bare `rm *` or `rm .*` --
# neither of which the old pattern caught. So 18 hand-written danger_notes had
# no delivery path at all. `search`, not `match`: the risky part is often the
# second half of a pipeline or a `&&` chain.
DANGEROUS = re.compile(
    r"""(^|[;&|]\s*)\s*
        # a BARE wildcard only: `rm *_spider_*` is the intended solution of
        # basic/08, while `rm *` in the same cellar destroys the signed bats
        (rm\s+(-[a-zA-Z]+\s+)*(\*|\.\*|/|~|\.\.)(\s|$)
        |rm\s+-[a-zA-Z]*r[a-zA-Z]*\s                  # any recursive rm
        |chmod\s+(-R\s+)?777
        |mv\s+\S+\s+/dev
        |>\s*/dev/sd)""",
    re.X)
ERROR_CLASSES = [
    ("no_such_file", re.compile(r"No such file or directory|Aucun fichier ou dossier", re.I)),
    ("permission", re.compile(r"Permission denied|Permission non accord", re.I)),
    ("not_found", re.compile(r"command not found|commande introuvable", re.I)),
    ("is_directory", re.compile(r"Is a directory|est un dossier|est un r.pertoire", re.I)),
]

# stuck-detection thresholds: hint level N is UNLOCKED at failed_attempts >= UNLOCK[N]
UNLOCK = {1: 0, 2: 2, 3: 4, 4: 6}

# Successful commands in a row that buy a rung back. The ladder used to be
# monotonic — nothing anywhere lowered hint_level, so one bad patch pinned a
# learner at "here is the literal answer" for the rest of that mission, and
# (because the level persists per mission number) for every replay of it.
DECAY_STREAK = 4

# A non-zero exit is not automatically a mistake.
#   130 = Ctrl-C, and intermediate/06 is a mission ABOUT pressing Ctrl-C
#   143 = SIGTERM, 141 = SIGPIPE (`… | head` closes the pipe early)
BENIGN_EXITS = {130, 141, 143}
# ...and for these, exit 1 is a legitimate answer ("no match", "they differ"),
# not a failure. Counting it as one escalated the hint ladder AND, because
# mastery required a lifetime error count of zero, permanently disqualified
# `grep` from ever being mastered after a single search that found nothing.
NO_MATCH_OK = {"grep", "egrep", "fgrep", "diff", "cmp", "test", "["}


# Operators the missions teach as concepts in their own right, spelled in
# missions_meta exactly as they appear here.
OPERATORS = ("2>", ">>", ">", "<", "|", "&", ";")
FIND_PREDICATES = ("-iname", "-name", "-type f", "-type d")


def concept_keys(cmd):
    """Every concept a single command line demonstrates.

    The learner model used to be keyed on argv[0] alone, so of the 97 concepts
    the missions name, the ~50 that are flags, operators or pipeline stages
    (`pipe |`, `ls -l`, `grep -l`, `cd ..`, `2>`) could never be marked
    mastered — which made `victory_mastered` unreachable for the entire
    pipes/redirection/permissions half of the game. A pipeline also only ever
    recorded its first command.
    """
    if not cmd:
        return []
    keys = []
    for op in OPERATORS:
        if op in cmd:
            keys.append(op)
    if "|" in cmd:
        keys.append("pipe |")
    for pred in FIND_PREDICATES:
        if pred in cmd:
            keys.append(pred)
    # every stage of a pipeline / chain, not just the first
    for stage in re.split(r"\||;|&&|\|\|", cmd):
        words = stage.split()
        while words:
            base = words[0]
            keys.append(base)
            # `xargs grep -l` really runs grep; so do `sudo`/`time`/`env`.
            # Without this the command that did the work is invisible, and
            # its flags would be misfiled under the wrapper.
            if base in ("xargs", "time", "nice", "env", "sudo", "command"):
                rest = words[1:]
                while rest and rest[0].startswith("-"):
                    rest = rest[1:]     # the wrapper's own flags
                words = rest
                continue
            for w in words[1:]:
                if not w.startswith("-") or w == "-":
                    continue
                keys.append("%s %s" % (base, w))
                # `ls -lA` also demonstrates `ls -l` and `ls -A`. Only for
                # short clusters: `find -name` is one long predicate, not
                # `-n -a -m -e`.
                if 2 < len(w) <= 4 and not w.startswith("--"):
                    for ch in w[1:]:
                        keys.append("%s -%s" % (base, ch))
            break
    if re.search(r"\bcd\s+\.\.", cmd):
        keys.append("cd ..")
    if re.search(r"\bcd\s+-\s*$", cmd):
        keys.append("cd -")
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def is_real_failure(cmd, exit_code):
    if exit_code in BENIGN_EXITS:
        return False
    base = (cmd or "").split()[0] if cmd else ""
    if exit_code == 1 and base in NO_MATCH_OK:
        return False
    return True


def classify_error(output):
    for name, rx in ERROR_CLASSES:
        if output and rx.search(output):
            return name
    return "generic"


class TutorEngine:
    def __init__(self, bridge, learner, llm, meta_dir, goals_cache, lang="en",
                 persona="socratic_diagnostician", no_probe=()):
        self.bridge = bridge
        self.learner = learner
        self.llm = llm
        self.meta_dir = meta_dir
        self.goals_cache = goals_cache
        self.lang = lang
        self.persona = persona
        self.current_mission = None
        self.mission_commands = []      # real commands run in current mission
        self.dialogue = []              # recent tutor/learner exchanges
        self.last_error_class = None
        self.knowledge = None           # RAG passages set by the daemon
        # Missions the shell cannot probe (their check asks the learner a
        # question, or waits on something) — see no-probe.list, built by
        # install.sh. Nothing will detect victory there, so the Game Master
        # says once that the learner has to submit with `gm fini`.
        self.no_probe = set(no_probe)
        self._nudged = set()
        # utterances produced since the caller last drained it (see _say)
        self.spoken = []

    def self_submit(self, mission_name):
        return bool(mission_name) and mission_name in self.no_probe

    # -- mission metadata ----------------------------------------------------
    def mission_meta(self, mission_name):
        if not mission_name:
            return {}
        path = os.path.join(self.meta_dir, mission_name.replace("/", "__") + ".json")
        try:
            with open(path) as f:
                return json.load(f)
        except OSError:
            return {}

    def mission_goal(self, mission_name):
        """Goal text pre-extracted at install time (mission files are
        protected/unreadable while the game runs)."""
        if not mission_name:
            return ""
        path = os.path.join(self.goals_cache, mission_name.replace("/", "__"),
                            self.lang + ".txt")
        if not os.path.exists(path):
            path = os.path.join(self.goals_cache, mission_name.replace("/", "__"),
                                "en.txt")
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return ""

    # -- context contract ------------------------------------------------------
    def build_context(self, kind, turn=None, **extra):
        mission_nb = (turn.mission if turn else self.current_mission) or "?"
        mission_name = self.bridge.mission_name(mission_nb)
        meta = self.mission_meta(mission_name)
        state = self.learner.mission_state(mission_nb)
        ctx = {
            "kind": kind,
            "lang": self.lang,
            "persona": self.persona,
            "mission_nb": mission_nb,
            "mission_name": mission_name,
            "mission_goal": self.mission_goal(mission_name),
            "mission_meta": meta,
            "mission_commands": self.mission_commands[-15:],
            "hint_level": state["hint_level"],
            "learner": self.learner.summary(),
            "dialogue": self.dialogue[-6:],
            "self_submit": self.self_submit(mission_name),
        }
        if turn is not None:
            ctx.update({
                "cmd": turn.cmd, "exit": turn.exit, "cwd": turn.cwd,
                "output": turn.output, "snapshot": turn.snapshot[:2000],
                "error_class": classify_error(turn.output) if turn.exit else None,
            })
        ctx.update(extra)
        if self.knowledge and kind in ("chat", "error"):
            ctx["knowledge"] = self.knowledge
        # latency: the per-call context is re-prefilled by the LLM every
        # time (only the baked system prompt is KV-cached) — send each kind
        # only what it needs
        if kind == "chat":
            ctx["mission_goal"] = (ctx.get("mission_goal") or "")[:400]
            ctx["snapshot"] = ""
            ctx["mission_commands"] = ctx["mission_commands"][-5:]
        elif kind == "error":
            ctx["snapshot"] = (ctx.get("snapshot") or "")[:600]
        return ctx

    def _say(self, ctx):
        t0 = time.time()
        reply = self.llm.respond(ctx)
        if reply:
            self.dialogue.append(["tutor", reply])
            # Record what was said and why. on_turn() returns bare strings, so
            # the kind, the hint level and the backend behind each utterance
            # were lost the moment it left here — the tutor's own half of the
            # session existed only as coloured text in the typescript, which
            # cannot be joined back to the turn that caused it. The daemon
            # drains this into tutor.jsonl.
            self.spoken.append({
                "kind": ctx.get("kind"),
                "mission_nb": ctx.get("mission_nb"),
                "mission_name": ctx.get("mission_name"),
                "hint_level": ctx.get("hint_level"),
                "persona": ctx.get("persona"),
                "error_class": ctx.get("error_class"),
                "ref_cmd": ctx.get("cmd"),
                "backend": getattr(self.llm, "last_route", None),
                "ms": int((time.time() - t0) * 1000),
                "text": reply,
            })
        return reply

    # -- main entry points -----------------------------------------------------
    def on_turn(self, turn):
        """Called by the pane for each completed real command. Returns a list
        of tutor utterances (possibly empty: silence is a feature)."""
        self.spoken = []
        out = []
        state = self.learner.mission_state(turn.mission)

        # mission change => greeting for the new mission
        if turn.mission != self.current_mission:
            self.current_mission = turn.mission
            self.mission_commands = []
            out.append(self._say(self.build_context("mission_start", turn)))

        base = (turn.cmd or "").split()[0] if turn.cmd else ""
        is_gsh = base == "gsh"
        if not is_gsh and base:
            self.mission_commands.append(turn.cmd)

        # Nothing will ever announce victory on a self-submit mission, so say
        # so — once, and only after the learner has actually done some work,
        # so it reads as guidance rather than as part of the briefing.
        mname = self.bridge.mission_name(turn.mission)
        if (self.self_submit(mname) and mname not in self._nudged
                and len(self.mission_commands) >= 3):
            self._nudged.add(mname)
            out.append(self._say(self.build_context("interactive_check", turn)))

        if is_gsh and turn.cmd.strip().startswith("gsh check"):
            # the ONLY source of truth for pass/fail is the engine itself
            passed = any(nb == str(turn.mission) and act == "CHECK_OK"
                         for nb, act in self.bridge.mission_log())
            if passed:
                # a solved mission starts clean if it is ever played again
                state["failed_attempts"] = 0
                state["streak"] = 0
                state["hint_level"] = 1
                out.append(self._say(self.build_context("check_pass", turn)))
            else:
                state["failed_attempts"] += 1
                state["streak"] = 0
                self._maybe_escalate(state)
                out.append(self._say(self.build_context("check_fail", turn)))
        elif turn.exit != 0 and base and is_real_failure(turn.cmd, turn.exit):
            err = classify_error(turn.output)
            self.learner.record_use(base, ok=False, error_class=err)
            state["failed_attempts"] += 1
            state["streak"] = 0
            # thrashing on the same error escalates faster
            if err == self.last_error_class:
                state["failed_attempts"] += 1
            self.last_error_class = err
            self._maybe_escalate(state)
            out.append(self._say(self.build_context("error", turn)))
        elif base and not is_gsh:
            # a non-zero exit that is not a mistake (Ctrl-C, `grep` with no
            # match) reaches here too: it is ordinary progress, not evidence
            # of being stuck, and must not climb the ladder
            # credit every concept the line demonstrated, not just argv[0].
            # Only on success: mastery is about what was shown to work, and
            # the error history stays keyed on the command that failed.
            for key in concept_keys(turn.cmd):
                self.learner.record_use(key, ok=(turn.exit == 0))
            self.last_error_class = None
            self._maybe_decay(state)
            if DANGEROUS.search(turn.cmd or ""):
                out.append(self._say(self.build_context("danger", turn)))

        self.learner.save()
        return [u for u in out if u]

    def _maybe_escalate(self, state):
        fails = state["failed_attempts"]
        for lvl in (2, 3, 4):
            if fails >= UNLOCK[lvl] and state["hint_level"] < lvl:
                state["hint_level"] = lvl
                break

    def _maybe_decay(self, state):
        """A run of commands that work is evidence the learner has found
        their footing: give a rung back, and let the ladder be climbed again
        on its own terms if they get stuck later."""
        state["streak"] = state.get("streak", 0) + 1
        if state["streak"] >= DECAY_STREAK and state["hint_level"] > 1:
            state["hint_level"] -= 1
            state["streak"] = 0
            # keep failed_attempts consistent with the level we just went
            # back to, or the next single failure would re-unlock everything
            state["failed_attempts"] = UNLOCK[state["hint_level"]]

    def on_idle(self):
        self.spoken = []
        state = self.learner.mission_state(self.current_mission or "?")
        # A long silence is weak evidence: it means "not typing", which is
        # also what reading the goal, thinking, or fetching a coffee looks
        # like. It used to add a rung unconditionally and say nothing about
        # it, so three idle timeouts walked a learner who had not failed once
        # all the way to the literal solution. Idle now counts as a single
        # failed attempt and obeys the same thresholds as everything else.
        state["failed_attempts"] += 1
        state["streak"] = 0
        self._maybe_escalate(state)
        self.learner.save()
        return self._say(self.build_context("idle"))

    def on_chat(self, message):
        self.spoken = []
        self.dialogue.append(["learner", message])
        if message.startswith("/hint"):
            # Asking twice at the same rung used to return the same sentence
            # verbatim, for as long as the learner kept asking. `hint_capped`
            # was written for exactly this and had no emitter anywhere.
            state = self.learner.mission_state(self.current_mission or "?")
            level = state["hint_level"]
            if state.get("hint_served_at") == level and level < 4:
                return self._say(self.build_context("hint_capped"))
            state["hint_served_at"] = level
            self.learner.save()
            return self._say(self.build_context("chat", message="hint request"))
        return self._say(self.build_context("chat", message=message))
