#!/usr/bin/env python3
"""Tutoring policy: the hint ladder, what counts as being stuck, and the
concept keys the learner model is built from.

engine.py is where CLAUDE.md says the policy lives, and it had no tests at
all. Everything asserted here is behaviour a learner feels directly, so it is
also the part most likely to be broken by a well-meaning edit.

Run: python3 test/test_engine_policy.py   (exits non-zero on failure)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "tutor"))

import engine as E                                          # noqa: E402
from engine import TutorEngine, concept_keys, is_real_failure, UNLOCK  # noqa: E402
from llm import MockLLMClient                               # noqa: E402

FAILURES = []


def check(label, cond):
    print("  %s %s" % ("ok  " if cond else "FAIL", label))
    if not cond:
        FAILURES.append(label)


class Turn:
    def __init__(self, cmd, exit=0, mission="1", output="", cwd="/home"):
        self.cmd, self.exit, self.mission = cmd, exit, mission
        self.output, self.cwd, self.snapshot = output, cwd, ""


class FakeBridge:
    """Only what the engine reads: the mission log and the mission name."""

    def __init__(self, name="basic/01_cd_tower"):
        self.name = name
        self.log = []

    def mission_log(self):
        return self.log

    def mission_name(self, nb):
        return self.name


class FakeLearner:
    def __init__(self):
        self.data = {"concepts": {}, "missions": {}, "error_patterns": []}
        self.uses = []

    def mission_state(self, nb):
        return self.data["missions"].setdefault(
            str(nb), {"hint_level": 1, "failed_attempts": 0})

    def record_use(self, cmd, ok, error_class=None):
        self.uses.append((cmd, ok))

    def summary(self):
        return {"mastered": [], "used": [], "recent_errors": []}

    def save(self):
        pass


def make_engine(meta=None, no_probe=()):
    eng = TutorEngine(FakeBridge(), FakeLearner(), MockLLMClient(),
                      meta_dir="/nonexistent", goals_cache="/nonexistent",
                      lang="en", no_probe=no_probe)
    if meta is not None:
        eng.mission_meta = lambda name: meta
    else:
        eng.mission_meta = lambda name: {}
    eng.mission_goal = lambda name: ""
    eng.current_mission = "1"
    return eng


HINTS = {"hints": {"en": ["CONCEPT", "LEAD", "SOLUTION"],
                   "fr": ["CONCEPT", "LEAD", "SOLUTION"]}}

print("== what counts as a real failure ==")
check("Ctrl-C (130) is not a mistake", not is_real_failure("sleep 100", 130))
check("SIGPIPE (141) is not a mistake", not is_real_failure("cat f | head", 141))
check("grep with no match is not a mistake", not is_real_failure("grep x f", 1))
check("diff reporting a difference is not a mistake",
      not is_real_failure("diff a b", 1))
check("a real command failure still is", is_real_failure("cd nowhere", 1))
check("grep failing for another reason still is",
      is_real_failure("grep x /root/secret", 2))

print("== the ladder goes up on evidence ==")
eng = make_engine(HINTS)
st = eng.learner.mission_state("1")
for i in range(UNLOCK[2]):
    eng.on_turn(Turn("cd nowhere", exit=1, output="No such file or directory"))
check("two real failures unlock rung 2", st["hint_level"] == 2)
for i in range(UNLOCK[4]):
    eng.on_turn(Turn("cd nowhere", exit=1, output="No such file or directory"))
check("persistent failure reaches rung 4", st["hint_level"] == 4)

print("== ...and it comes back down ==")
eng = make_engine(HINTS)
st = eng.learner.mission_state("1")
st["hint_level"], st["failed_attempts"] = 3, 4
for _ in range(E.DECAY_STREAK):
    eng.on_turn(Turn("ls"))
check("a run of working commands gives a rung back", st["hint_level"] == 2)
check("failed_attempts follows the level down",
      st["failed_attempts"] == UNLOCK[2])
check("one later failure does not jump straight back to 3",
      (eng.on_turn(Turn("cd nowhere", exit=1, output="No such file")),
       st["hint_level"] == 2)[1])

print("== a benign non-zero exit is not evidence ==")
eng = make_engine(HINTS)
st = eng.learner.mission_state("1")
for _ in range(6):
    eng.on_turn(Turn("grep needle haystack", exit=1))
check("six fruitless greps do not unlock anything", st["hint_level"] == 1)
check("...and do not count as attempts", st["failed_attempts"] == 0)
eng2 = make_engine(HINTS)
st2 = eng2.learner.mission_state("1")
for _ in range(6):
    eng2.on_turn(Turn("sleep 100", exit=130))
check("nor does pressing Ctrl-C six times", st2["hint_level"] == 1)

print("== a solved mission starts clean if replayed ==")
eng = make_engine(HINTS)
st = eng.learner.mission_state("1")
st["hint_level"], st["failed_attempts"] = 4, 9
eng.bridge.log = [("1", "CHECK_OK")]
eng.on_turn(Turn("gsh check", mission="1"))
check("check_pass resets the ladder", st["hint_level"] == 1)
check("check_pass resets the attempt count", st["failed_attempts"] == 0)

print("== idle is weak evidence, not a free rung ==")
eng = make_engine(HINTS)
st = eng.learner.mission_state("1")
eng.on_idle()
check("one idle timeout does not unlock rung 2", st["hint_level"] == 1)
for _ in range(UNLOCK[4] + 2):
    eng.on_idle()
check("idling alone still obeys the thresholds", st["hint_level"] <= 4)
check("...and gets there only after enough of them",
      st["failed_attempts"] >= UNLOCK[4])

print("== the diagnosis is never replaced by the hint ==")
eng = make_engine(HINTS)
st = eng.learner.mission_state("1")
st["hint_level"] = 3
out = eng.on_turn(Turn("cat Cave", exit=1, output="cat: Cave: Is a directory"))
said = "\n".join(out)
check("the shell's own message is still read back", "Is a directory" in said)
check("and the earned rung is added to it", "LEAD" in said)
check("but not a rung above it", "SOLUTION" not in said)

print("== repeated hint requests are capped ==")
eng = make_engine(HINTS)
eng.learner.mission_state("1")["hint_level"] = 2
first = eng.on_chat("/hint")
second = eng.on_chat("/hint")
check("the first request serves the rung", "CONCEPT" in (first or ""))
check("the second does not just repeat it", "CONCEPT" not in (second or ""))
check("it says to try a little more", bool(second))

print("== dangerous commands reach their authored note ==")
eng = make_engine({"danger_note": "THE BATS ARE SIGNED"})
said = "\n".join(eng.on_turn(Turn("rm *")))
check("`rm *` is caught at all", bool(said))
check("the mission's own danger_note is spoken", "THE BATS ARE SIGNED" in said)
check("and the recovery path is named", "gm reset" in said)
check("the intended `rm *_spider_*` is NOT flagged",
      not "\n".join(make_engine({"danger_note": "N"})
                    .on_turn(Turn("rm *_spider_*"))))

print("== concept keys cover what the missions actually name ==")
k = concept_keys("ls -lA")
check("`ls -l` is credited from `ls -lA`", "ls -l" in k and "ls -A" in k)
k = concept_keys("find . -name '*.txt' | xargs grep -l motif")
check("every stage of a pipeline counts",
      all(c in k for c in ("find", "xargs", "grep")))
check("`grep -l` is credited", "grep -l" in k)
check("`pipe |` is credited", "pipe |" in k and "|" in k)
check("find predicates are credited", "-name" in k)
check("redirections are credited", "2>" in concept_keys("cmd 2> /dev/null"))
check("`cd ..` is credited", "cd .." in concept_keys("cd .."))
check("plain `cd` is not mistaken for `cd ..`",
      "cd .." not in concept_keys("cd Castle"))

print("== a self-submit mission is announced, once ==")
eng = make_engine(HINTS, no_probe=["basic/01_cd_tower"])
said = []
for _ in range(5):
    said += eng.on_turn(Turn("ls"))
nudges = [s for s in said if "gm fini" in s]
check("the learner is told to submit", len(nudges) == 1)
eng2 = make_engine(HINTS)
said2 = []
for _ in range(5):
    said2 += eng2.on_turn(Turn("ls"))
check("but not on an auto-detected mission",
      not [s for s in said2 if "gm fini" in s])

print()
if FAILURES:
    print("FAILED: %d" % len(FAILURES))
    for f in FAILURES:
        print("  - %s" % f)
    sys.exit(1)
print("ALL ENGINE POLICY TESTS PASSED")
