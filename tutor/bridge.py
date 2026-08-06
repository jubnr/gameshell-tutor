# bridge.py — SessionBridge (tutor side). GPLv3.
#
# Reads what the shell-side shim recorded (turns.jsonl + session.json) and
# the typescript produced by script(1), and assembles complete Turn objects:
# command, exit code, cwd, mission, filesystem snapshot, and the EXACT
# stdout/stderr captured between the shim's OSC markers.
#
# The bridge never interprets anything: it only reports what really happened.

import json
import os
import re

PRE_MARK = re.compile(rb"\x1b\]777;gshtutor;pre;(\d+)\x07")
POST_MARK = re.compile(rb"\x1b\]777;gshtutor;post;(\d+);(-?\d+)\x07")
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")
CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

OUTPUT_CAP = 4000  # chars of real output given to the LLM per turn


class Turn:
    def __init__(self, tid, cmd, cwd):
        self.id = tid
        self.cmd = cmd
        self.cwd = cwd
        self.exit = None
        self.mission = None
        self.snapshot = ""
        self.output = None  # None = not captured (no typescript); "" = empty

    def done(self):
        return self.exit is not None


def clean_output(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n")
    text = ANSI.sub("", text)
    text = CTRL.sub("", text)
    if len(text) > OUTPUT_CAP:
        half = OUTPUT_CAP // 2
        text = text[:half] + "\n[... output truncated ...]\n" + text[-half:]
    return text.strip("\n")


class SessionBridge:
    """Tails turns.jsonl and the typescript of one game session."""

    def __init__(self, session_dir):
        self.dir = session_dir
        self.turns_path = os.path.join(session_dir, "turns.jsonl")
        self.typescript_path = os.path.join(session_dir, "typescript")
        self.session = {}
        self._offset = 0
        self._open_turns = {}
        sess_file = os.path.join(session_dir, "session.json")
        if os.path.exists(sess_file):
            with open(sess_file) as f:
                self.session = json.load(f)

    # -- typescript slicing ------------------------------------------------
    def _output_for(self, tid):
        if not os.path.exists(self.typescript_path):
            return None
        with open(self.typescript_path, "rb") as f:
            data = f.read()
        pre = re.search(rb"\x1b\]777;gshtutor;pre;%d\x07" % tid, data)
        post = re.search(rb"\x1b\]777;gshtutor;post;%d;-?\d+\x07" % tid, data)
        if not pre:
            return None
        end = post.start() if post else len(data)
        return clean_output(data[pre.end():end])

    # -- event stream ------------------------------------------------------
    def poll(self):
        """Return a list of newly completed Turns (and session events)."""
        events = []
        if not os.path.exists(self.turns_path):
            return events
        with open(self.turns_path) as f:
            f.seek(self._offset)
            new = f.read()
            self._offset = f.tell()
        for line in new.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue  # line still being written; picked up next poll
            kind = ev.get("event")
            if kind == "session_start":
                events.append(("session_start", ev))
            elif kind == "pre":
                self._open_turns[ev["id"]] = Turn(ev["id"], ev["cmd"], ev["cwd"])
            elif kind == "post":
                turn = self._open_turns.pop(ev["id"], None)
                if turn is None:
                    continue
                turn.exit = ev["exit"]
                turn.mission = str(ev.get("mission", ""))
                turn.cwd = ev.get("cwd", turn.cwd)
                turn.snapshot = ev.get("snapshot", "")
                turn.output = self._output_for(turn.id)
                events.append(("turn", turn))
        return events

    # -- game progression (read-only, from the engine's own files) ----------
    def mission_log(self):
        """[(mission_nb, action)] from the engine's missions.log (source of truth)."""
        path = os.path.join(self.session.get("gsh_config", ""), "missions.log")
        entries = []
        try:
            with open(path) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0][0].isdigit():
                        entries.append((parts[0], parts[1]))
        except OSError:
            pass
        return entries

    def mission_name(self, nb):
        """Map mission number -> mission dir name via the engine's index.idx."""
        path = os.path.join(self.session.get("gsh_config", ""), "index.idx")
        try:
            with open(path) as f:
                names = [l.strip() for l in f
                         if l.strip() and not l.strip().startswith("!")]
            n = int(nb)
            if 1 <= n <= len(names):
                return names[n - 1]
        except (OSError, ValueError):
            pass
        return None
