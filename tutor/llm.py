# llm.py — LLM client interface, deterministic mock, and HTTP client. GPLv3.
#
# THE MASTER RULE (encoded here and in the HTTP system prompt):
#   The shell executes and judges; the LLM only interprets.  Every claim must
#   be grounded in the actual captured output/state present in the context.
#   When output was not captured, tell the learner to run it and observe.
#   Mission success is decided by `gsh check` — never by the LLM.
#
# MockLLMClient is rule-based and fully offline: the tutor MUST work on it.
# HttpLLMClient posts to an OpenAI-compatible /v1/chat/completions endpoint.
# Nothing is hardcoded; backends are selected by config/env:
#
#   backend "mock"   — default, no network at all
#   backend "http"   — generic endpoint:
#       GSH_TUTOR_LLM_URL    base URL or full endpoint
#                            (http://host:8000, .../v1, .../v1/chat/completions)
#       GSH_TUTOR_LLM_MODEL  model name
#       GSH_TUTOR_LLM_KEY    optional bearer token
#   backend "ollama" — local Ollama, OpenAI-compatible API. Defaults:
#       base_url http://localhost:11434/v1, model "shell-tutor" (the
#       Modelfile-built tutor, see ollama/Modelfile), api key "not-needed"
#       (Ollama ignores it). Overrides:
#       GSH_TUTOR_OLLAMA_HOST  e.g. http://<gpu-host-ip>:11434  (lab setup)
#       GSH_TUTOR_LLM_MODEL / GSH_TUTOR_LLM_KEY as above.
#       The shell-tutor model BAKES the system prompt (master rule + context
#       contract + hint policy) in its Modelfile, so in this mode the client
#       sends no system message: the dynamic state (persona, hint_level,
#       mastered concepts) already travels in the per-turn context JSON.
#
# Backend selection order (resolve_backend): GSH_TUTOR_LLM_BACKEND env >
# "llm" key in config.json > "http" if GSH_TUTOR_LLM_URL is set > "mock".

import json
import os
import re
import urllib.request


# --------------------------------------------------------------------------
# mock templates (fr/en); {placeholders} are filled ONLY with captured data
# --------------------------------------------------------------------------
T = {
    "fr": {
        "greet": "[{name}] Une nouvelle épreuve t'attend, aventurier·ère. Demande-la moi : `gm mission`. {intent_line}",
        "greet_generic": "[mission {nb}] Une nouvelle épreuve t'attend, aventurier·ère — demande-la moi avec `gm mission` et explore avec `ls`.",
        "brief_intro": "⚔ Approche, aventurier·ère — voici ta nouvelle épreuve :",
        "brief_outro": "Des questions ? Tape `gm` (puis Entrée) et parle-moi librement. J'annoncerai moi-même ta victoire dès que l'épreuve sera accomplie.",
        "err_no_such_file": "Le shell répond : « {errline} ». Il ne trouve pas ce chemin depuis `{cwd}`. Que te montre `ls` ici ? Le fichier est-il vraiment là, sous ce nom exact ?",
        "err_permission": "« {errline} » — un problème de droits, pas d'existence. Regarde la colonne des permissions : que dit `ls -l` sur ce fichier ?",
        "err_not_found": "« {errline} » — le shell ne connaît pas cette commande. Vérifie l'orthographe. (Tape-la puis regarde ce que le shell répond, c'est lui qui a raison.)",
        "err_is_dir": "« {errline} » — la cible est un répertoire, pas un fichier. Quelle commande s'applique aux répertoires ici ?",
        "err_generic": "La commande a échoué (code {exit}). Lis le message exact :\n  {errline}\nQue t'apprend-il sur la cause ?",
        "check_fail": "L'épreuve n'est pas encore accomplie :\n  {output}\nCompare ce qui est attendu avec l'état réel — que te montre `ls` à l'endroit concerné ?",
        "check_pass": "🎉 Épreuve accomplie, aventurier·ère ! {idiom}",
        # the Game Master's own word on a victory: one opening line (varied so
        # it never reads like a canned receipt), then observations drawn ONLY
        # from what really happened this mission
        "victory": [
            "🎉 Épreuve accomplie, aventurier·ère ! Le château retient ton nom.",
            "🎉 Victoire ! Tu as plié cette épreuve comme un vieux parchemin.",
            "🎉 Voilà qui est fait, aventurier·ère. Le shell t'a obéi.",
            "🎉 Bien joué ! Une épreuve de plus derrière toi.",
            "🎉 Accompli ! Je note ton passage dans les registres du donjon.",
            "🎉 Épreuve remportée. Le royaume te doit une fière chandelle.",
        ],
        "victory_swift": "En {n} commandes seulement : de la belle ouvrage.",
        "victory_mastered": "Tu manies {cmds} sans hésiter désormais.",
        "victory_persevered": "Il t'a fallu t'accrocher, et tu t'es accroché·e. C'est cela qui compte.",
        "idle": "Toujours là, aventurier·ère ? Si tu es bloqué·e, tape `gm` et dis-moi ce que tu essaies d'obtenir, ou demande `gm indice`.",
        "run_and_see": "Je ne vais pas te le dire : lance la commande et observe la sortie réelle — c'est le shell qui fait foi. Reviens me dire ce que tu vois.",
        "no_output": "(sortie non capturée pour ce tour — je ne peux pas la commenter; relance si besoin)",
        "chat_redirect": "Bonne question. Avant que je réponde : que te dit le dernier message du shell ? Décris-le moi.",
        "hint_capped": "Essaie encore un peu par toi-même, aventurier·ère — je monte d'un cran si tu bloques vraiment (ou après quelques essais).",
        "danger": "⚠ Prudence : cette commande modifie/supprime des fichiers de façon irréversible. Vérifie d'abord la cible avec `ls` avant de l'exécuter.",
        "mastered_skip": "",
        "llm_fallback": "[tuteur hors-ligne : le LLM distant n'a pas répondu, je continue en mode mock]",
    },
    "en": {
        "greet": "[{name}] A new trial awaits you, adventurer. Ask me for it: `gm mission`. {intent_line}",
        "greet_generic": "[mission {nb}] A new trial awaits you, adventurer — ask me with `gm mission` and explore with `ls`.",
        "brief_intro": "⚔ Come closer, adventurer — here is your new trial:",
        "brief_outro": "Questions? Type `gm` (then Enter) and talk to me freely. I will proclaim your victory myself as soon as the trial is accomplished.",
        "err_no_such_file": "The shell says: \"{errline}\". It cannot find that path from `{cwd}`. What does `ls` show here? Is the file really there, under that exact name?",
        "err_permission": "\"{errline}\" — a permission problem, not a missing file. Look at the permission column: what does `ls -l` say about it?",
        "err_not_found": "\"{errline}\" — the shell does not know that command. Check the spelling. (The shell's answer is the truth.)",
        "err_is_dir": "\"{errline}\" — the target is a directory, not a file. Which command applies to directories here?",
        "err_generic": "The command failed (exit {exit}). Read the exact message:\n  {errline}\nWhat does it tell you about the cause?",
        "check_fail": "The trial is not accomplished yet:\n  {output}\nCompare what is expected with the real state — what does `ls` show in the relevant place?",
        "check_pass": "🎉 Trial accomplished, adventurer! {idiom}",
        "victory": [
            "🎉 Trial accomplished, adventurer! The castle remembers your name.",
            "🎉 Victory! You folded that trial like an old parchment.",
            "🎉 It is done, adventurer. The shell obeyed you.",
            "🎉 Well played! One more trial behind you.",
            "🎉 Accomplished! I am entering your passage in the keep's registers.",
            "🎉 Trial won. The realm owes you a debt.",
        ],
        "victory_swift": "In only {n} commands: fine work.",
        "victory_mastered": "You wield {cmds} without hesitation now.",
        "victory_persevered": "You had to keep at it, and you did. That is what counts.",
        "idle": "Still there, adventurer? If you are stuck, type `gm` and tell me what you are trying to achieve, or ask `gm indice`.",
        "run_and_see": "I won't tell you: run the command and observe the real output — the shell is the source of truth. Then tell me what you see.",
        "no_output": "(output not captured for this turn — I cannot comment on it; run it again if needed)",
        "chat_redirect": "Good question. Before I answer: what does the shell's last message say? Describe it to me.",
        "hint_capped": "Try a little more on your own — I will step up the help if you are genuinely stuck (or after a few attempts).",
        "danger": "⚠ Careful: this command modifies/deletes files irreversibly. Check the target with `ls` before running it.",
        "mastered_skip": "",
        "llm_fallback": "[tutor offline: remote LLM did not answer, continuing on the mock]",
    },
}

# Engine commands quoted inside mission goal texts, rewritten to the `gm`
# interface that replaces them in the Game Master frontend. Every `gsh …` that
# actually occurs in the shipped goals is listed here — check with:
#   grep -rhoE "gsh [A-Za-z_]+" ~/.local/share/gameshell-tutor/goals-cache/*/*.txt
# FINAL_MISSION is the one that quotes HELP/index/goto, and `HELP` is
# upper-case there, so it needs its own entry (the replace is literal).
# Longest-first: "gsh goal" must not be eaten by a "gsh go" style prefix.
GM_COMMAND_MAP = (
    ("gsh check", "gm fini"),
    ("gsh reset", "gm reset"),
    ("gsh goal", "gm mission"),
    ("gsh index", "gm index"),
    ("gsh goto", "gm goto"),
    # HELP asks for "the list of all commands", so it maps to the GM's own
    # listing (shim: _tutor_gm_help), not to bare `gm` which only opens a
    # dialogue prompt and would make the sentence untrue
    ("gsh HELP", "gm commandes"),
    ("gsh help", "gm commandes"),
)

ERROR_TEMPLATES = {
    "no_such_file": "err_no_such_file",
    "permission": "err_permission",
    "not_found": "err_not_found",
    "is_directory": "err_is_dir",
}


def first_error_line(output):
    for line in (output or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


class LLMClient:
    def respond(self, ctx):
        raise NotImplementedError


class MockLLMClient(LLMClient):
    """Deterministic, offline, rule-based tutor voice (socratic persona)."""

    def respond(self, ctx):
        t = T.get(ctx.get("lang", "en"), T["en"])
        kind = ctx["kind"]
        meta = ctx.get("mission_meta") or {}
        level = ctx.get("hint_level", 1)

        if kind == "mission_start":
            # the Game Master narrates the mission himself: the REAL goal
            # text (ground truth, from the mission's own goal file) framed
            # in character — never a paraphrase that could drop a detail
            goal = (ctx.get("mission_goal") or "").strip()
            if goal:
                lines = goal.splitlines()
                while lines and (set(lines[0].strip()) <= {"="}
                                 or lines[0].strip().lower()
                                 in ("objectif", "mission goal", "obiettivo")):
                    lines.pop(0)
                body = "\n".join(lines).strip()
                # goal texts sometimes teach the engine's own gsh commands;
                # the GM interface renames them (content stays verbatim)
                for old, new in GM_COMMAND_MAP:
                    body = body.replace(old, new)
                # page breaks (\x06): the GM pauses before each section and
                # waits for the learner (space) — RPG-dialogue style
                blines = body.splitlines()
                paged = []
                for i, bl in enumerate(blines):
                    nxt = blines[i + 1].strip() if i + 1 < len(blines) else ""
                    if paged and nxt and set(nxt) <= {"=", "-"}:
                        paged.append("\x06")
                    paged.append(bl)
                body = "\n".join(paged)
                name = ctx.get("mission_name") \
                    or "mission %s" % ctx.get("mission_nb", "?")
                intent_line = (meta.get("intent_lang") or {}).get(
                    ctx.get("lang"), "")
                parts = [t["brief_intro"].format(name=name), "", body, "",
                         "\x06"]
                if intent_line:
                    parts += [intent_line, ""]
                parts.append(t["brief_outro"])
                return "\n".join(parts)
            if meta.get("intent"):
                return t["greet"].format(
                    name=ctx.get("mission_name", "?"),
                    intent_line=meta["intent_lang"].get(ctx.get("lang"), ""))
            return t["greet_generic"].format(nb=ctx.get("mission_nb", "?"))

        if kind == "error":
            # graded ladder: mission-specific hints (from meta) override the
            # generic socratic templates from level 2 upward
            hints = (meta.get("hints") or {}).get(ctx.get("lang")) \
                or (meta.get("hints") or {}).get("en") or []
            if level >= 2 and len(hints) >= level - 1:
                return hints[level - 2]
            key = ERROR_TEMPLATES.get(ctx.get("error_class"), "err_generic")
            errline = first_error_line(ctx.get("output"))
            if not errline and ctx.get("output") is None:
                return t["no_output"]
            return t[key].format(errline=errline, cwd=ctx.get("cwd", "?"),
                                 exit=ctx.get("exit"))

        if kind == "check_fail":
            return t["check_fail"].format(
                output=first_error_line(ctx.get("output")) or "(no message)")

        if kind == "check_pass":
            # The Game Master says a word of his own on every victory. It is
            # rendered here, not by the LLM: victory has to land in the same
            # short prompt window as the next mission's briefing, and every
            # line below is grounded in captured data (the commands really
            # run, the learner model, this mission's metadata).
            run = [c for c in (ctx.get("mission_commands") or []) if c.strip()]
            variants = t.get("victory") or [t["check_pass"].format(idiom="")]
            # deterministic pick: the same mission always gets the same
            # opening, so a replay reads identically (hash() is randomised)
            seed = sum(ord(c) for c in str(ctx.get("mission_nb", "")))
            parts = [variants[seed % len(variants)]]

            if len(run) and len(run) <= 3:
                parts.append(t["victory_swift"].format(n=len(run)))
            elif ctx.get("hint_level", 1) >= 3 or len(run) >= 8:
                # they struggled and got there anyway: say so, it matters more
                # than the shortcut they could have taken
                parts.append(t["victory_persevered"])

            # concepts this mission teaches that the learner has now mastered
            mastered = set((ctx.get("learner") or {}).get("mastered") or [])
            earned = [c for c in (meta.get("concepts") or []) if c in mastered]
            if earned:
                parts.append(t["victory_mastered"].format(
                    cmds=", ".join("`%s`" % c for c in earned[:3])))

            review = meta.get("idiom_review", {}).get(ctx.get("lang")) if meta else None
            if review:
                # only offer the pro version if the learner's real commands
                # match the "naive pattern" this mission's meta describes
                pattern = meta.get("idiom_trigger", "")
                if not pattern or pattern in " ; ".join(run):
                    parts.append(review)
            return "\n".join(parts).strip()

        if kind == "danger":
            return t["danger"]

        if kind == "idle":
            return t["idle"]

        if kind == "hint_capped":
            return t["hint_capped"]

        if kind == "chat":
            msg = (ctx.get("message") or "").lower()
            hints = (meta.get("hints") or {}).get(ctx.get("lang")) \
                or (meta.get("hints") or {}).get("en") or []
            if msg == "hint request" and hints:
                # an explicit hint request serves the current rung of the
                # mission's curated ladder (concept at levels 1-2)
                return hints[min(max(level - 2, 0), len(hints) - 1)]
            if any(w in msg for w in ("what does", "que fait", "qu'est-ce que",
                                      "c'est quoi", "output", "affiche")):
                return t["run_and_see"]
            if level >= 2 and len(hints) >= level - 1:
                return hints[level - 2]
            return t["chat_redirect"]

        return None


# Sent only by the generic "http" backend. The ollama backend uses the model's
# BAKED prompt instead (ollama/Modelfile) — keep the two in sync when editing
# the rules below.
SYSTEM_PROMPT = """You are "le Maître du Jeu", a game-master character living
inside GameShell's castle world, acting as shell tutor (stay in character,
brief and warm; never mention being an AI). MASTER RULE:
the shell executes and judges; you only interpret. NEVER invent what a command
does or fabricate output — every claim must be grounded in the captured
output/state in the context below. If output was not captured, say so and ask
the learner to run it and observe. Never present a destructive command as safe
without a real check having been captured.

Mission success is detected automatically by the game — never claim a mission
passed or failed yourself (the context `kind` carries the verdict), and never
tell the learner to run `gsh check`, `gsh goal` or any other `gsh` command.
Your replies are plain prose: never begin a reply with "gm" and never write
`gm ...` lines — `gm` is what the LEARNER types to reach you. The only things
you may point them to are `gm` (ask a question), `gm indice` (a hint),
`gm mission` (re-hear the quest).

CONTEXT: the user message is one JSON object — cmd/exit/cwd/output/snapshot
(what the shell really did; `output` null means nothing was captured),
mission_goal and mission_meta.intent (the objective; you never see the check
script), mission_commands, learner.mastered, dialogue, and `knowledge`
(excerpts from this machine's man pages — authoritative, prefer them over
your own memory). mission_meta.hints_unlocked, when present, holds the
mission's curated hints ALREADY filtered to the level you are allowed to
give; there is never a further rung to reveal.

Persona: {persona}. Answer in language: {lang}.
Graded help — current allowed hint level {level}/4 (1 nudge/question,
2 concept, 3 concrete lead, 4 the command). Do NOT exceed it, and never give
the answer first. Never re-explain concepts the learner has mastered:
{mastered}. Keep answers short (<= 6 lines)."""

PERSONAS = {
    "socratic_diagnostician": "Socratic diagnostician: lead with questions grounded in the exact error/state; make the learner reason about a command before running it.",
    "intent_scaffolder": "Intent scaffolder: the learner states a goal in natural language; decompose it into subgoals; each step is tested in the real shell before the next.",
    "apprentice_to_debug": "Apprentice: you play a plausible beginner; you PROPOSE (never execute) slightly wrong commands the learner must diagnose using real shell results.",
    "postmortem_narrator": "Postmortem narrator: stay silent during play; when a mission passes, replay the recorded commands and critique the path taken.",
    "adversary": "Adversary: between missions you propose (never execute) environment breaks and coach the repair; the shell verifies the repair.",
}


OLLAMA_DEFAULT_BASE = "http://localhost:11434"
OLLAMA_DEFAULT_MODEL = "shell-tutor"


GM_ECHO = re.compile(r"^\s*[`*]*\s*gm\s*[`*]*\s*[.:;!?]*\s*$", re.I)


def strip_gm_echo(text):
    """Drop lines that are nothing but a bare `gm`.

    The baked prompt has to name `gm` repeatedly (it is how the learner
    speaks to the Game Master), which primes the token hard. When the model
    reaches for a command example but the hint level forbids giving one, it
    emits the primed token alone — "Commencez par vous rendre au jardin :"
    followed by a lone ``gm``, a command placeholder that means nothing to
    the learner. The prompt already forbids it; a 7B model obeys that only
    most of the time, so this is the deterministic backstop."""
    if not text:
        return text
    kept = [l for l in text.splitlines() if not GM_ECHO.match(l)]
    # collapse the blank line the removal may leave behind
    out = []
    for line in kept:
        if not line.strip() and out and not out[-1].strip():
            continue
        out.append(line)
    return "\n".join(out).strip()


def allowed_hints(ctx):
    """The rungs of the mission's curated ladder that the current hint_level
    has unlocked, in the learner's language: level 1 none, 2 the concept,
    3 + the concrete lead, 4 + the command. Same mapping the mock uses
    (hints[level - 2] is the rung for `level`), so both voices stay in step."""
    meta = ctx.get("mission_meta") or {}
    hints = (meta.get("hints") or {}).get(ctx.get("lang")) \
        or (meta.get("hints") or {}).get("en") or []
    try:
        level = int(ctx.get("hint_level", 1))
    except (TypeError, ValueError):
        level = 1
    return hints[:max(0, level - 1)]


def redact_context(ctx):
    """The context as sent to a REMOTE model — never the mock's.

    mission_meta on disk carries the mission's whole curated ladder, rung 4
    included ("The command: `cd ~/Chateau/...`"), plus the post-victory idiom
    review, in every language. Handing all of that to the model puts the
    solution inside the prompt and then asks it politely not to look — and it
    does look: at hint_level 1, on the learner's very first question, it hands
    over the full answer. So the gating happens here, at the network edge:
    the mock runs in-process and picks its own rung, only the remote model
    needs the ladder cut to size.

    Dropping the unreachable rungs and the other language also cuts ~600
    prompt tokens per call, which on a CPU-bound local model is most of the
    per-turn prefill (measured 24s -> 6.6s on qwen2.5:7b, CPU)."""
    meta = ctx.get("mission_meta") or {}
    if not meta:
        return ctx
    lang = ctx.get("lang", "en")
    slim = {}
    if meta.get("intent"):
        # success as INTENT: legitimate grounding, not the literal check
        slim["intent"] = meta["intent"]
    intent_line = (meta.get("intent_lang") or {}).get(lang)
    if intent_line:
        slim["intent_lang"] = intent_line
    hints = allowed_hints(ctx)
    if hints:
        slim["hints_unlocked"] = hints
    if ctx.get("kind") == "danger" and meta.get("danger_note"):
        # what is actually irreversible in THIS mission, and what the check
        # will not forgive. It gives nothing away (it is a warning, not a
        # solution) and it is the whole point of the danger kind; before the
        # redaction it reached the model only by accident, inside the raw meta
        slim["danger_note"] = meta["danger_note"]
    if ctx.get("kind") == "check_pass":
        # the idiomatic alternative is a post-victory reveal: it gives nothing
        # away once the mission is won, and no other kind has a use for it
        review = (meta.get("idiom_review") or {}).get(lang)
        if review:
            slim["idiom_review"] = review
    out = dict(ctx)
    out["mission_meta"] = slim
    return out


def chat_completions_url(url):
    """Accept a bare host, a /v1 base, or a full endpoint; return the full
    OpenAI-style /v1/chat/completions endpoint."""
    url = url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


class HttpLLMClient(LLMClient):
    # optional streaming: when `chunk_sink` is set (an object with
    # .feed(delta) and .close()), requests use SSE streaming and every
    # content delta is fed to the sink as it arrives; respond() still
    # returns the full text at the end.
    chunk_sink = None

    def __init__(self, base_url=None, model=None, key=None,
                 baked_system=False, fallback=None):
        url = base_url or os.environ.get("GSH_TUTOR_LLM_URL", "")
        self.url = chat_completions_url(url) if url else ""
        self.model = model or os.environ.get("GSH_TUTOR_LLM_MODEL", "")
        self.key = key if key is not None else os.environ.get("GSH_TUTOR_LLM_KEY", "")
        # True when the model itself carries the tutor system prompt
        # (Modelfile SYSTEM): sending another one would override or duplicate
        # the frozen prompt, so we send only the context JSON.
        self.baked_system = baked_system
        self.fallback = fallback or MockLLMClient()

    def respond(self, ctx):
        if not self.url:
            return self.fallback.respond(ctx)
        messages = []
        if not self.baked_system:
            messages.append({"role": "system", "content": SYSTEM_PROMPT.format(
                persona=PERSONAS.get(ctx.get("persona", ""),
                                     PERSONAS["socratic_diagnostician"]),
                lang=ctx.get("lang", "en"),
                level=ctx.get("hint_level", 1),
                mastered=", ".join(
                    ctx.get("learner", {}).get("mastered", [])) or "none",
            )})
        messages.append({"role": "user", "content": json.dumps(
            redact_context(ctx), ensure_ascii=False)})
        # short replies: briefings never go through the LLM. `gm` waits up to
        # 60s (cold model load) and the prompt hook up to 12s, so the timeout
        # below must stay under the former for the mock fallback to land.
        sink = self.chunk_sink
        payload = {"model": self.model, "messages": messages,
                   "max_tokens": 220, "temperature": 0.3,
                   "stream": bool(sink)}
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": "Bearer " + self.key} if self.key else {})})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                if not sink:
                    data = json.load(resp)
                    return strip_gm_echo(
                        data["choices"][0]["message"]["content"])
                # SSE: one "data: {...}" line per delta, "data: [DONE]" ends
                full = []
                for raw in resp:
                    raw = raw.decode("utf-8", errors="replace").strip()
                    if not raw.startswith("data:"):
                        continue
                    raw = raw[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        delta = (json.loads(raw)["choices"][0]
                                 .get("delta", {}).get("content") or "")
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if delta:
                        full.append(delta)
                        sink.feed(delta)
                # the sink filters each chunk as it goes, so the text stored
                # for take() must be filtered the same way or the daemon will
                # treat the reply as unstreamed and post it a second time
                text = strip_gm_echo("".join(full))
                sink.close(text)
                return text
        except Exception:
            if sink:
                sink.close(None)  # abandon any partial stream cleanly
            t = T.get(ctx.get("lang", "en"), T["en"])
            mock = self.fallback.respond(ctx)
            return (t["llm_fallback"] + "\n" + mock) if mock else t["llm_fallback"]


def resolve_backend(config=None):
    """GSH_TUTOR_LLM_BACKEND env > config 'llm' key > 'http' if a URL is
    set > 'mock'."""
    backend = os.environ.get("GSH_TUTOR_LLM_BACKEND", "")
    if not backend and config:
        backend = config.get("llm", "")
    if not backend and os.environ.get("GSH_TUTOR_LLM_URL"):
        backend = "http"
    return backend or "mock"


def make_client(mode):
    if mode == "ollama":
        return HttpLLMClient(
            base_url=os.environ.get("GSH_TUTOR_OLLAMA_HOST",
                                    OLLAMA_DEFAULT_BASE),
            model=os.environ.get("GSH_TUTOR_LLM_MODEL", OLLAMA_DEFAULT_MODEL),
            key=os.environ.get("GSH_TUTOR_LLM_KEY", "not-needed"),
            baked_system=True)
    if mode == "http":
        return HttpLLMClient()
    return MockLLMClient()
