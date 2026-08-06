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
./install.sh <archive.sh | game_dir>   # shim + goal texts + check sandbox list
./play.sh --pane                        # older tmux side-panel frontend

python3 test/test_llm_backends.py       # LLM wiring, against a fake local server
python3 test/replay.py <session-dir> ["message" ...]   # replay a session on the mock

python3 tutor/rag.py build              # rebuild the RAG index
./ollama/build.sh                       # rebuild the shell-tutor model
sudo bash ollama/enable-igpu.sh [vulkan|rocm|--revert]
```

There is no lint step, no package manager and no build system. `test/` files
are plain scripts, not a framework: run them directly, they exit non-zero on
failure. `test_llm_backends.py` is the closest thing to a unit suite and is
where new LLM-boundary behaviour belongs.

## Architecture

Data flows one way, and the shell is always the authority:

```
shim (bash)  ->  turns.jsonl / chat.jsonl  ->  SessionBridge  ->  TutorEngine
                                                                      |
outbox/*.msg  <-  Outbox/StreamSink  <-  LLMClient (mock | http | ollama)
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
  only sees complete files) and signals the shell with SIGUSR1.
- `tutor/tutor_pane.py` is the alternative tmux-pane frontend and also owns
  `load_config` / `TUTOR_HOME`, which the daemon imports.

Runtime state lives outside the repo, in
`~/.local/share/gameshell-tutor/`: `config.json`, `sessions/<stamp>/`,
`goals-cache/`, the learner model, and the RAG index. `GSH_TUTOR_HOME` moves
all of it, which is how `subject.sh` isolates participants: each gets its own
tutor home AND its own copy of the game, because GameShell keeps progress
inside the archive and the learner model is keyed on the game's
`$GSH_CONFIG/uid`.

## Invariants that are easy to break

**The shell judges, never the LLM.** Mission success is read from the
engine's `missions.log`. The shim evaluates a mission's own `check.sh` in a
subshell and, only on success, triggers the real check (see the subshell
caveat below). Do not add a code path where the model decides an outcome.

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
fingerprint covers the briefing templates, `GM_COMMAND_MAP` and
`MISSION_ART`; changing any of them retires old files automatically. If you
add another input to a briefing, add it to that fingerprint too.

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

**A subshell does not contain filesystem writes.** The shim auto-detects
success by sourcing each mission's `check.sh` after every command. Several
GameShell checks clean up the player's world on their *failure* path:
`basic/06_mv_coins_garden` deletes the three coins it is about (its own source
says `#FIXME: use clean.sh`), which made the mission unwinnable. In every such
check the cleanup sits on the *failure* path, after the verdict is decided, so
the probe can keep its answer while losing its bite: `install.sh` lists the
offenders in `$TUTOR_HOME/sandbox-check.list` and creates no-op `rm`/`mv`/`cp`
stubs in `$TUTOR_HOME/noop-bin`, which the shim puts first on `PATH` for those
missions only. Auto-detection stays on everywhere. The stubs must shadow on
PATH, not as shell functions: the checks pipe through `xargs -0 rm`, and xargs
execs `rm` itself. Recovery from a wiped mission is `gm reset`, never
re-creating props by hand: they are signed with `sign_file` and `check_file`
rejects anything else.

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
`danger` kind. `open_solution` is authored in a few missions but no code path
reads it yet.

