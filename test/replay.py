#!/usr/bin/env python3
# replay.py — offline test: drive the tutor (mock LLM) over a session dir,
# real (produced by the shim during play) or synthetic. Prints the tutor
# transcript. GPLv3.
#
# Usage: replay.py <session_dir> [chat message injected at the end ...]

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tutor"))
from bridge import SessionBridge          # noqa: E402
from engine import TutorEngine            # noqa: E402
from learner_model import LearnerModel    # noqa: E402
from llm import MockLLMClient             # noqa: E402


def main():
    session_dir = sys.argv[1]
    bridge = SessionBridge(session_dir)
    lang = (bridge.session.get("lang") or "en")[:2]
    learner = LearnerModel(gsh_config=None, durable_home=session_dir,
                           uid="replay")
    engine = TutorEngine(
        bridge, learner, MockLLMClient(),
        meta_dir=os.path.join(HERE, "..", "tutor", "missions_meta"),
        goals_cache=os.path.join(
            os.environ.get("GSH_TUTOR_HOME",
                           os.path.expanduser("~/.local/share/gameshell-tutor")),
            "goals-cache"),
        lang=lang)

    for kind, payload in bridge.poll():
        if kind != "turn":
            continue
        print("$ %s   [exit %s]" % (payload.cmd, payload.exit))
        if payload.output:
            print("  | " + payload.output.replace("\n", "\n  | ")[:400])
        for utterance in engine.on_turn(payload):
            print("  🎓 " + utterance.replace("\n", "\n     "))
        print()

    for msg in sys.argv[2:]:
        print("[learner] " + msg)
        reply = engine.on_chat(msg)
        if reply:
            print("  🎓 " + reply.replace("\n", "\n     "))
        print()

    print("--- learner model summary ---")
    import json
    print(json.dumps(learner.summary(), indent=1, ensure_ascii=False))
    print("hint levels:", {k: v["hint_level"]
                           for k, v in learner.data["missions"].items()})


if __name__ == "__main__":
    main()
