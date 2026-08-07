#!/usr/bin/env python3
# replay.py — offline: drive the tutor (mock LLM) over a session dir, real
# (produced by the shim during play) or synthetic. Prints the tutor
# transcript. GPLv3.
#
# Usage: replay.py <session_dir> [chat message injected at the end ...]
#
# STRICTLY READ-ONLY on the session. It used to build its LearnerModel with
# durable_home=<the session dir>, which made every "analysis" run write a
# learner-replay.json into the participant data it was reading; four such
# files were sitting in real sessions. It also read goals-cache from the
# ambient GSH_TUTOR_HOME rather than the one the session was played under,
# so replaying a subject.sh session narrated from the wrong goal texts.

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tutor"))
from bridge import SessionBridge          # noqa: E402
from engine import TutorEngine            # noqa: E402
from learner_model import LearnerModel    # noqa: E402
from llm import MockLLMClient             # noqa: E402


class ReadOnlyLearner(LearnerModel):
    """A learner model that accumulates in memory and never writes."""

    def __init__(self):
        super().__init__(gsh_config=None, durable_home=None, uid=None)

    def save(self):
        pass


def tutor_home_for(session_dir):
    """The tutor home this session was played under.

    A session lives at <TUTOR_HOME>/sessions/<stamp>/, so it names its own
    home. subject.sh gives every participant a separate one; taking it from
    the environment instead loaded another subject's goal texts, or none.
    """
    parent = os.path.dirname(os.path.dirname(os.path.abspath(session_dir)))
    if os.path.isdir(os.path.join(parent, "goals-cache")):
        return parent
    return os.environ.get(
        "GSH_TUTOR_HOME",
        os.path.expanduser("~/.local/share/gameshell-tutor"))


def main():
    if len(sys.argv) < 2:
        print("usage: replay.py <session_dir> [message ...]", file=sys.stderr)
        return 2
    session_dir = sys.argv[1]
    if not os.path.isdir(session_dir):
        print("no such session dir: %s" % session_dir, file=sys.stderr)
        return 2

    home = tutor_home_for(session_dir)
    bridge = SessionBridge(session_dir)
    lang = (bridge.session.get("lang") or "en")[:2]
    goals_cache = os.path.join(home, "goals-cache")
    no_probe = []
    try:
        with open(os.path.join(home, "no-probe.list")) as f:
            no_probe = [l.strip() for l in f if l.strip()]
    except OSError:
        pass

    engine = TutorEngine(
        bridge, ReadOnlyLearner(), MockLLMClient(),
        meta_dir=os.path.join(HERE, "..", "tutor", "missions_meta"),
        goals_cache=goals_cache, lang=lang, no_probe=no_probe)

    # A replay is only as good as the metadata it can resolve. Say so loudly
    # rather than producing a plausible transcript with mission_name = None
    # throughout, which is what happened once the game tree was deleted.
    if not os.path.isdir(goals_cache):
        print("! no goals-cache under %s: briefings will be generic" % home,
              file=sys.stderr)

    turns = bridge.poll()
    named = 0
    for kind, payload in turns:
        if kind != "turn":
            continue
        if bridge.mission_name(payload.mission):
            named += 1
        print("$ %s   [exit %s]" % (payload.cmd, payload.exit))
        if payload.output:
            print("  | " + payload.output.replace("\n", "\n  | ")[:400])
        for utterance in engine.on_turn(payload):
            print("  🎓 " + utterance.replace("\n", "\n     "))
        print()

    if turns and not named:
        print("! no mission could be named: the game tree is gone and this "
              "session has no progress/ copy (played before it was kept?)",
              file=sys.stderr)

    for msg in sys.argv[2:]:
        print("[learner] " + msg)
        reply = engine.on_chat(msg)
        if reply:
            print("  🎓 " + reply.replace("\n", "\n     "))
        print()

    print("--- learner model summary ---")
    print(json.dumps(engine.learner.summary(), indent=1, ensure_ascii=False))
    print("hint levels:", {k: v["hint_level"]
                           for k, v in engine.learner.data["missions"].items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
