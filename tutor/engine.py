# engine.py — TutorEngine: per-turn context building and tutoring policy. GPLv3.
#
# Policy lives here (when to speak, hint ladder, stuck detection, learner
# model updates); wording lives in the LLM client (mock or HTTP).
# The engine never runs commands and never decides mission success —
# it only reads what the shell already did.

import json
import os
import re

DANGEROUS = re.compile(r"^\s*(rm\s+-rf?\s+[/~*]|rm\s+-rf?\s+\.\.|chmod\s+777|mv\s+.*\s+/dev)")
ERROR_CLASSES = [
    ("no_such_file", re.compile(r"No such file or directory|Aucun fichier ou dossier", re.I)),
    ("permission", re.compile(r"Permission denied|Permission non accord", re.I)),
    ("not_found", re.compile(r"command not found|commande introuvable", re.I)),
    ("is_directory", re.compile(r"Is a directory|est un dossier|est un r.pertoire", re.I)),
]

# stuck-detection thresholds: hint level N is UNLOCKED at failed_attempts >= UNLOCK[N]
UNLOCK = {1: 0, 2: 2, 3: 4, 4: 6}


def classify_error(output):
    for name, rx in ERROR_CLASSES:
        if output and rx.search(output):
            return name
    return "generic"


class TutorEngine:
    def __init__(self, bridge, learner, llm, meta_dir, goals_cache, lang="en",
                 persona="socratic_diagnostician"):
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
        reply = self.llm.respond(ctx)
        if reply:
            self.dialogue.append(["tutor", reply])
        return reply

    # -- main entry points -----------------------------------------------------
    def on_turn(self, turn):
        """Called by the pane for each completed real command. Returns a list
        of tutor utterances (possibly empty: silence is a feature)."""
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

        if is_gsh and turn.cmd.strip().startswith("gsh check"):
            # the ONLY source of truth for pass/fail is the engine itself
            passed = any(nb == str(turn.mission) and act == "CHECK_OK"
                         for nb, act in self.bridge.mission_log())
            if passed:
                state["failed_attempts"] = 0
                out.append(self._say(self.build_context("check_pass", turn)))
            else:
                state["failed_attempts"] += 1
                self._maybe_escalate(state)
                out.append(self._say(self.build_context("check_fail", turn)))
        elif turn.exit != 0 and base:
            err = classify_error(turn.output)
            self.learner.record_use(base, ok=False, error_class=err)
            state["failed_attempts"] += 1
            # thrashing on the same error escalates faster
            if err == self.last_error_class:
                state["failed_attempts"] += 1
            self.last_error_class = err
            self._maybe_escalate(state)
            out.append(self._say(self.build_context("error", turn)))
        elif base and not is_gsh:
            self.learner.record_use(base, ok=True)
            self.last_error_class = None
            if DANGEROUS.match(turn.cmd or ""):
                out.append(self._say(self.build_context("danger", turn)))

        self.learner.save()
        return [u for u in out if u]

    def _maybe_escalate(self, state):
        fails = state["failed_attempts"]
        for lvl in (2, 3, 4):
            if fails >= UNLOCK[lvl] and state["hint_level"] < lvl:
                state["hint_level"] = lvl
                break

    def on_idle(self):
        state = self.learner.mission_state(self.current_mission or "?")
        # long idle is a genuine stuck signal: unlock one more level
        if state["hint_level"] < 4:
            state["hint_level"] += 1
            self.learner.save()
        return self._say(self.build_context("idle"))

    def on_chat(self, message):
        self.dialogue.append(["learner", message])
        if message.startswith("/hint"):
            return self._say(self.build_context("chat", message="hint request"))
        return self._say(self.build_context("chat", message=message))
