# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An LLM tutor layered **on top of** an unmodified GameShell (a bash learning
game). A character called "le Maître du Jeu" / "the Game Master" narrates
missions, diagnoses real shell errors and answers questions. The game engine
is never edited: the tutor is injected as a shell shim plus an out-of-process
Python daemon.

All learner-facing content is bilingual (`fr` / `en`), keyed off `$LANG`.

## Commands

```sh
./play.sh                              # play; bundled game in game/, no setup
./subject.sh <id>                      # play as a named subject, state isolated
./play.sh <archive.sh | game_dir>      # use a different GameShell
./install.sh <archive.sh | game_dir>   # shim + goal texts + no-probe list + stubs
./play.sh --pane                        # older tmux side-panel frontend

python3 test/test_llm_backends.py       # LLM wiring, against a fake local server
python3 test/test_engine_policy.py      # hint ladder, stuck detection, concept keys
python3 test/replay.py <session-dir> ["message" ...]   # replay a session on the mock

python3 tutor/rag.py build              # rebuild the RAG index
./ollama/build.sh                       # rebuild the shell-tutor model
sudo bash ollama/enable-igpu.sh [vulkan|rocm|--revert]
```

There is no lint step, no package manager and no build system. `test/` files
are plain scripts, not a framework: run them directly, they exit non-zero on
failure (`replay.py` is an inspection tool, not a test — it prints a
transcript and never writes into the session it reads).
`test_llm_backends.py` owns the LLM boundary (redaction, briefings, backends,
mock coverage); `test_engine_policy.py` owns policy (ladder, stuck detection,
`concept_keys`, danger). New behaviour belongs in whichever of the two matches.

## Hard environment requirements

These are the things that break on a different machine, so check them before
debugging anything else:

- **bash 5.1+**: the shim assigns `PROMPT_COMMAND` as an ARRAY
  (`shim/gshrc_tutor.sh`, near the bottom). On 5.0 and older that silently
  becomes a string and the whole shim stops firing.
- **python3 3.9+**: `rag.py` uses `str.removesuffix`.
- **util-linux `script`**: `play.sh` runs `script -qf FILE -c CMD`. The BSD /
  macOS `script` takes different arguments; without it there is no typescript
  and the tutor sees no command output at all.
- Everything else is coreutils. `numpy` is optional (RAG only, degrades to no
  RAG), `tmux` only for `--pane`, `git` only to stamp `condition.json`.

## Architecture

Data flows one way, and the shell is always the authority:

```
shim (bash)  ->  turns.jsonl / chat.jsonl  ->  SessionBridge  ->  TutorEngine
                                                                      |
outbox/*.msg  <-  Outbox/StreamSink  <-  LLMClient (mock | http | ollama)
     |                  |
  shim prints        Journal -> tutor.jsonl   (what was said, and why)
```

- `shim/gshrc_tutor.sh` runs **inside** the game. It records every command,
  exit code, cwd and a bounded filesystem snapshot, and defines the `gm`
  function. It is inert unless `GSH_TUTOR=1`. It never calls an LLM.
- `tutor/bridge.py` tails those logs and slices exact per-command output from
  the `script(1)` typescript using invisible OSC markers.
- `tutor/engine.py` holds **policy**: when to speak, the hint ladder, stuck
  detection, learner-model updates. It builds the context dict.
- `tutor/llm.py` holds **wording**: the deterministic `MockLLMClient` and the
  `HttpLLMClient`. Backend order: `GSH_TUTOR_LLM_BACKEND` > `llm` key in
  config > `http` if a URL is set > `mock`.
- `tutor/tutor_daemon.py` is the default frontend. It renders each utterance
  into one file under `$SESSION/outbox/` (written tmp-then-rename so the shim
  only sees complete files) and signals the shell with SIGUSR1. It also owns
  the briefing cache, the `pending` lease, the read cursor and the `Journal`.
- `tutor/learner_model.py` is the durable record of what the learner has
  shown: `seen / used / mastered` per concept key, with a clean-run `streak`
  (mastery is about the recent record, not a spotless lifetime one).
- `tutor/tutor_pane.py` is the alternative tmux-pane frontend and also owns
  `load_config` / `TUTOR_HOME`, which the daemon imports.

Runtime state lives outside the repo, in
`~/.local/share/gameshell-tutor/`: `config.json`, `sessions/<stamp>/`,
`goals-cache/`, `no-probe.list`, `noop-bin/`, the learner model, and the RAG
index. A session directory holds `turns.jsonl` + `chat.jsonl` (the learner's
side), `tutor.jsonl` + `condition.json` (the tutor's side), the `script(1)`
typescript, `cursor.json`, and a `progress/` copy of the engine's
`missions.log`/`index.idx` so the session stays readable after the game tree
is deleted. `GSH_TUTOR_HOME` moves
all of it, which is how `subject.sh` isolates participants: each gets its own
tutor home AND its own copy of the game, because GameShell keeps progress
inside the archive and the learner model is keyed on the game's
`$GSH_CONFIG/uid`.

## The utterance contract

`engine.build_context(kind, ...)` builds one dict per utterance; every `kind`
must have a branch in `MockLLMClient.respond`, and `test_engine_policy.py` +
`test_llm_backends.py` enforce that together.

| kind | when |
|---|---|
| `mission_start` | mission changed (the daemon usually pre-empts this with its cached briefing) |
| `error` | a command failed *and* `is_real_failure()` agrees it was a mistake |
| `check_pass` / `check_fail` | the engine's verdict, read from `missions.log` |
| `danger` | a command matched `DANGEROUS` and succeeded |
| `idle` | no activity for `IDLE_SECONDS` |
| `chat` | `gm <text>`; `message="hint request"` for `gm indice` |
| `hint_capped` | a second hint request at a rung already served |
| `interactive_check` | first few commands of a mission the shell cannot probe |

`FastVerdictClient.FAST_KINDS` routes everything except free-text `chat` and
`error` to the mock, whatever the configured backend — verdicts must land in
the same prompt window, and a briefing must never be improvised. That means a
remote model only ever sees two kinds; anything you add to `redact_context`
for another kind is unreachable in the default setup.

## Delivery markers

The daemon puts invisible control characters in a message to direct the shim's
printer. They must be stripped on **every** delivery path — the paced loop and
the `GSH_TUTOR_PACE=0` fast path — or a learner sees raw control bytes.

| char | meaning |
|---|---|
| `\x06` | page break: wait for a keypress before the next section |
| `\x0e` | this line is scenery (ASCII art): draw it with no delay, and no paragraph pause after the block |

## Environment variables

`GSH_TUTOR_HOME` (all state), `GSH_TUTOR_LLM_BACKEND` / `_URL` / `_MODEL` /
`_KEY`, `GSH_TUTOR_OLLAMA_HOST`, `GSH_TUTOR_EMBED_MODEL`, `GSH_TUTOR_PACE`
(0 = no typewriter), `GSH_TUTOR_ART` (0 = no scenery), `GSH_TUTOR_PROBE_TIMEOUT`
(seconds, default 2), `GSH_TUTOR_FRONTEND` (`pane`), `GSH_TUTOR_ROOT` (where
the shim finds this repo), `GSH_TUTOR_SESSION` (set by `play.sh`),
`GSH_SUBJECTS` (participant root).

## Invariants that are easy to break

**The shell judges, never the LLM.** Mission success is read from the
engine's `missions.log`. The shim evaluates a mission's own `check.sh` in a
sandboxed, time-bounded subshell and, only on success, triggers the real
check (see the probe caveats below). Do not add a code path where the model
decides an outcome.

**`llm.redact_context()` is the hint-leak boundary.** `mission_meta` on disk
contains the full curated hint ladder, rung 4 of which is the literal
solution. The mock runs in-process and picks its own rung, so it receives
everything; the remote model must only ever receive `hints_unlocked`, the
rungs the current `hint_level` has earned. Anything added to `mission_meta`
is invisible to the model unless explicitly allowed through here.

**`ollama/Modelfile` needs a rebuild to take effect.** The system prompt is
baked into the model, so editing the file changes nothing until
`./ollama/build.sh` runs. A stale model will happily advertise commands that
no longer exist. In `ollama` mode the client sends **no** system message
(`baked_system=True`); dynamic state travels in the per-turn context JSON.

**Mission briefings are rendered by the mock, never the LLM**, because a
briefing must reproduce the goal's operational details verbatim. They are
cached in `goals-cache/narration-<mission>.<lang>.<fingerprint>.txt`. The
fingerprint covers the briefing templates, `GM_COMMAND_MAP`, `MISSION_ART`,
the goal-sentence rewrites, `NARRATION_FORMAT` and whether `GSH_TUTOR_ART=0`;
changing any of them retires old files automatically. If you add another
input to a briefing, add it to that fingerprint too — the art flag was
missing, so one artless session poisoned the cache for every later one.

**Goal texts are not in this repo.** Mission files are chmod-protected while
the game runs, so `install.sh` pre-extracts them into `goals-cache/` at
install time. The game directory is unreadable during a live session.

**`game/gameshell.sh` is the bundled engine and must stay pristine.**
`install.sh` patches archives in place and the engine rewrites them on
autosave, so `play.sh` always plays from a `.game/` copy (gitignored). The
bundled archive already contains the shim, so the shim-mismatch check alone
would skip `install.sh` on a fresh clone and leave `goals-cache/` empty:
`play.sh` therefore also forces an install when this machine has no runtime
state yet.

**Streaming has two output paths.** With a `chunk_sink`, `StreamSink` posts
sentence-sized chunks live, and `respond()` also returns the full text. The
daemon calls `sink.take(text)` to avoid posting twice, so any filtering
applied to a chunk must be applied identically to the returned text, or
replies get duplicated.

**Never run the real `gsh check` speculatively.** The engine's failure path
(`lib/gsh.sh`, `_gsh_check`) sources `clean.sh`, logs `CHECK_OOPS`, autosaves,
and calls `__gsh_start` — which re-runs `init.sh` and **resets the mission's
world**. `missions.log` is checksum-chained, so a `CHECK_OOPS` written by
mistake can never be corrected, only avoided. The shim used to auto-run the
real check every third command on the ten missions whose `check.sh` reads an
answer from the learner; for eight of them each run re-randomised the very
secret the learner was holding, and two (`stdin_stdout_stderr/02` and `/05`)
could never pass from a terminal at all. The only trigger for a real check is
now a passed silent probe, or the learner's own `gm fini`.

**A subshell does not contain filesystem writes, so every probe is
sandboxed.** The shim auto-detects success by sourcing each mission's
`check.sh` after every command. Several GameShell checks clean up the
player's world on their *failure* path: `basic/06_mv_coins_garden` deletes
the three coins it is about (its own source says `#FIXME: use clean.sh`), and
`intermediate/04_bg_xeyes` ends both failure branches with `xargs kill -9` on
the process the mission asks the learner to keep running. The cleanup always
sits *after* the verdict, so the probe can keep its answer while losing its
bite: `_tutor_probe` runs every check with the no-op stubs of
`$TUTOR_HOME/noop-bin` first on `PATH`, `enable -n kill`, and `TMPDIR`
pointed outside the game tree (the game's `mktemp` writes into `$GSH_ROOT`,
and leaked temp files would be repacked into every autosave). This used to be
a blacklist of "unsafe" checks; a blacklist over hand-written shell always
has a hole, and the costs are not symmetric — over-sandboxing loses an
auto-detection, under-sandboxing destroys the learner's work.
The stubs must shadow on PATH, not as shell functions: the checks pipe
through `xargs -0 rm`, and xargs execs `rm` itself. `kill` needs both,
because it is also a builtin. Recovery from a wiped mission is `gm reset`,
never re-creating props by hand: they are signed with `sign_file` and
`check_file` rejects anything else.

**`$TUTOR_HOME/no-probe.list` marks the missions nothing can detect.** Built
by `scan_unsafe_checks`: checks that `read` an answer from the learner (the
stdin-closed probe can never pass them, and `permissions/03` spins forever on
read-at-EOF) and checks that wait (`processes/03_pstree_kill` busy-waits on a
file with no timeout of its own; `intermediate/05_background` sleeps 2s).
Those get no probe at all — the engine emits the `interactive_check` kind
once, and the briefing keeps its `gm fini` instructions instead of promising
an automatic victory. `_tutor_probe` also enforces a wall-clock budget and
drops a mission after two timeouts, as a backstop for checks the scanner
misses.

**A dead daemon must not degrade the shell silently.** `$SESSION/pending`
tells the prompt hook to hold back while an answer is being prepared. It
carries the daemon's pid and a deadline, and the shim honours it only while
both hold: without that, a daemon killed mid-iteration left the marker behind
and every subsequent prompt paid the full 12s cap, silently, forever.
`$SESSION/cursor.json` records how far turns/chat have been consumed, so the
daemon `gm` respawns resumes instead of replaying the whole session's
briefings and diagnoses onto the next prompt. `Outbox` also resumes its
numbering from the spool, or new messages would sort before undelivered ones.

**What the tutor says is recorded in `$SESSION/tutor.jsonl`.** One line per
utterance: kind, mission, `hint_level`, persona, the backend that actually
produced it (`FastVerdictClient` sends most kinds to the mock whatever the
config says), latency and text. `engine._say` collects them and the daemon
drains them; `condition.json` records the session's backend/model/persona/RAG
so a run can be identified afterwards. Without these a session recorded only
the learner's half, and `hint_level` — the dose variable — existed only as a
final value in `learner_model.json`.

**The daemon is long-running.** Code changes need a new session; `play.sh`
respawns it and refreshes the embedded shim.

**Launching from a game directory needs `GSH_EXEC_DIR` / `GSH_EXEC_FILE`.**
They are normally exported by the self-extracting archive's `lib/header.sh`,
which is skipped when running `start.sh` directly. Without them the engine's
autosave builds its path from empty strings and fails at the filesystem root.
`play.sh` supplies them.

**The mock must always work.** It is the default backend and the fallback on
any network failure, so the game never breaks. Every `kind` the engine emits
needs a mock branch.

## Conventions

Python is stdlib-only except `numpy` (optional, RAG only); a missing numpy
degrades to no RAG rather than failing. Shell targets POSIX `sh` where the
game does, bash where the shim does.

Comments explain *why*, especially where behaviour looks odd but is
deliberate (anti-cheat workarounds, delivery timing, cache invalidation).
Match that when editing.

`tutor/missions_meta/*.json` is hand-written pedagogical content: `intent`,
`intent_lang`, a 3-rung `hints` ladder per language, `idiom_review`,
`idiom_trigger`. 44 of the 45 missions are covered; `FINAL_MISSION` is the
closing screen and needs none. `danger_note` reaches the model only on the
`danger` kind, and is spoken by the mock's `danger` branch. `open_solution`
marks missions whose check accepts several routes: the last rung is then
served with a caveat instead of as *the* answer. `idiom_trigger` may be a
string or a per-language dict, and a learner who repeated one command three
times qualifies anyway — six of the triggers are French world literals
(`cd Chateau`, `grep diamant`), which an English learner never types.

