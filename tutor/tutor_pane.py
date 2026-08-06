#!/usr/bin/env python3
# tutor_pane.py — terminal front-end for the GameShell tutor. GPLv3.
#
# Runs BESIDE the game terminal (play.sh puts them in a tmux split).
# Left pane: the real GameShell shell (untouched, runs under script(1)).
# This pane: tutor chat. The learner types here to talk to the tutor
# (/hint, /persona, or free text). The tutor NEVER executes
# commands — the learner always types commands themselves, in the game pane.

import json
import os
import select
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge import SessionBridge            # noqa: E402
from engine import TutorEngine              # noqa: E402
from learner_model import LearnerModel      # noqa: E402
from llm import make_client, resolve_backend, PERSONAS  # noqa: E402

TUTOR_HOME = os.environ.get(
    "GSH_TUTOR_HOME",
    os.path.join(os.path.expanduser("~"), ".local/share/gameshell-tutor"))
IDLE_SECONDS = 180

CYAN, YELLOW, DIM, RESET = "\033[36m", "\033[33m", "\033[2m", "\033[0m"


def say(text, color=CYAN):
    print("%s🎓 %s%s\n" % (color, text.replace("\n", "\n   "), RESET), flush=True)


def load_config():
    cfg = {"frontend": "terminal_pane", "persona": "socratic_diagnostician",
           "llm": "mock"}
    path = os.path.join(TUTOR_HOME, "config.json")
    try:
        with open(path) as f:
            cfg.update(json.load(f))
    except (OSError, json.JSONDecodeError):
        pass
    cfg["llm"] = resolve_backend(cfg)
    return cfg


def wait_for_session(session_dir=None):
    pointer = os.path.join(TUTOR_HOME, "current-session")
    print(DIM + "waiting for a GameShell session (GSH_TUTOR=1)..." + RESET,
          flush=True)
    while True:
        d = session_dir
        if d is None and os.path.exists(pointer):
            with open(pointer) as f:
                d = f.read().strip()
        if d and os.path.exists(os.path.join(d, "session.json")):
            return d
        time.sleep(0.5)


def main():
    cfg = load_config()
    if cfg["frontend"] == "godot":
        print("frontend=godot is not implemented yet; falling back to "
              "terminal_pane.")
    session_dir = wait_for_session(sys.argv[1] if len(sys.argv) > 1 else None)
    bridge = SessionBridge(session_dir)
    sess = bridge.session
    lang = (sess.get("lang") or "en")[:2]
    tutor_root = os.path.dirname(os.path.abspath(__file__))
    learner = LearnerModel(sess.get("gsh_config"), TUTOR_HOME, sess.get("uid"))
    engine = TutorEngine(
        bridge, learner, make_client(cfg["llm"]),
        meta_dir=os.path.join(tutor_root, "missions_meta"),
        goals_cache=os.path.join(TUTOR_HOME, "goals-cache"),
        lang=lang, persona=cfg["persona"])

    say("Tuteur prêt (LLM: %s, persona: %s). Joue dans l'autre panneau ; "
        "parle-moi ici. Commandes: /hint, /persona <nom>."
        % (cfg["llm"], cfg["persona"]) if lang == "fr" else
        "Tutor ready (LLM: %s, persona: %s). Play in the other pane; talk to "
        "me here. Commands: /hint, /persona <name>."
        % (cfg["llm"], cfg["persona"]))

    last_activity = time.time()
    idle_nudged = False
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], 0.5)
        if ready:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            last_activity, idle_nudged = time.time(), False
            if line.startswith("/persona"):
                name = line.split(None, 1)[1].strip() if " " in line else ""
                if name in PERSONAS:
                    engine.persona = name
                    say("persona -> " + name, YELLOW)
                else:
                    say("personas: " + ", ".join(PERSONAS), YELLOW)
                continue
            reply = engine.on_chat(line)
            if reply:
                say(reply)

        for kind, payload in bridge.poll():
            last_activity, idle_nudged = time.time(), False
            if kind == "turn":
                print("%s$ %s  [exit %s]%s" % (DIM, payload.cmd, payload.exit,
                                               RESET), flush=True)
                for utterance in engine.on_turn(payload):
                    say(utterance)

        if (time.time() - last_activity > IDLE_SECONDS and not idle_nudged
                and engine.current_mission):
            idle_nudged = True
            reply = engine.on_idle()
            if reply:
                say(reply, YELLOW)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
