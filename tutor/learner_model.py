# learner_model.py — persistent model of what the learner has demonstrated. GPLv3.
#
# Two copies are kept in sync:
#  - $GSH_CONFIG/tutor/learner_model.json : rides GameShell's own savefile
#    mechanism (colocated with the engine's progression data);
#  - ~/.local/share/gameshell-tutor/learner-<GSH_UID>.json : survives the
#    deletion of the extracted game directory even without a savefile.
# On load, the most recently updated copy wins.

import json
import os
import time

STATUS_ORDER = ["seen", "used", "mastered"]
MASTERY_USES = 3  # successful uses with no recent error => mastered


def _blank():
    return {
        "updated": 0,
        "concepts": {},        # cmd -> {status, uses, errors, last_error}
        "error_patterns": [],  # recent [(error_class, cmd)] (bounded)
        "missions": {},        # mission_nb -> {hint_level, failed_attempts}
    }


class LearnerModel:
    def __init__(self, gsh_config, durable_home, uid):
        self.paths = []
        if gsh_config:
            self.paths.append(os.path.join(gsh_config, "tutor", "learner_model.json"))
        if durable_home and uid:
            self.paths.append(os.path.join(durable_home, "learner-%s.json" % uid))
        self.data = _blank()
        for p in self.paths:
            try:
                with open(p) as f:
                    d = json.load(f)
                if d.get("updated", 0) > self.data.get("updated", 0):
                    self.data = d
            except (OSError, json.JSONDecodeError):
                pass

    def save(self):
        self.data["updated"] = int(time.time())
        blob = json.dumps(self.data, indent=1, ensure_ascii=False)
        for p in self.paths:
            try:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(blob)
            except OSError:
                pass

    # -- concepts ----------------------------------------------------------
    def concept(self, cmd):
        c = self.data["concepts"].setdefault(
            cmd, {"status": "seen", "uses": 0, "errors": 0, "last_error": None})
        c.setdefault("streak", 0)   # models written before streaks existed
        return c

    def record_use(self, cmd, ok, error_class=None):
        c = self.concept(cmd)
        if ok:
            c["uses"] += 1
            c["streak"] += 1
            # Mastery is about the RECENT record, not a spotless lifetime one.
            # Requiring errors == 0 forever meant a single early stumble --
            # or a `grep` that simply found nothing -- barred a command from
            # ever being called mastered again, however well it was used
            # afterwards. A clean run of MASTERY_USES is the evidence.
            if c["uses"] >= MASTERY_USES and c["streak"] >= MASTERY_USES:
                c["status"] = "mastered"
            elif c["status"] == "seen":
                c["status"] = "used"
        else:
            c["errors"] += 1
            c["streak"] = 0
            c["last_error"] = error_class
            if c["status"] == "mastered":
                c["status"] = "used"  # regression signal
        if error_class:
            self.data["error_patterns"].append([error_class, cmd])
            self.data["error_patterns"] = self.data["error_patterns"][-20:]

    def mastered(self, cmd):
        return self.data["concepts"].get(cmd, {}).get("status") == "mastered"

    # -- per-mission tutoring state -----------------------------------------
    def mission_state(self, nb):
        return self.data["missions"].setdefault(
            str(nb), {"hint_level": 1, "failed_attempts": 0})

    def summary(self):
        """Compact summary for the LLM context."""
        by_status = {}
        for cmd, c in self.data["concepts"].items():
            by_status.setdefault(c["status"], []).append(cmd)
        return {
            "mastered": sorted(by_status.get("mastered", [])),
            "used": sorted(by_status.get("used", [])),
            "recent_errors": self.data["error_patterns"][-5:],
        }
