# gameshell-tutor

An LLM tutor that lives **inside** [GameShell](https://github.com/phyver/GameShell),
playing the part of **the Game Master** 🧙. It narrates each mission, explains
your real errors and answers your questions — without ever typing a command
for you.

The whole idea in one line: **the shell executes and judges, the LLM only
interprets.** The GameShell engine itself is never modified.

```
[mission 5] $ rm spider_1
rm: cannot remove 'spider_1': Permission denied

🧙 The Game Master — (about `rm spider_1`)
   "Permission denied" is a rights problem, not a missing file.
   What does `ls -l` say about it?

[mission 5] $ gm hint
```

It works **completely offline**, with no LLM and no network, out of the box.
Adding a local model is optional.

---

## 1. Requirements

Tested on Linux. Everything below is standard on a normal desktop distro.

| Needed | Why | Check with |
|---|---|---|
| **bash 5.1 or newer** | the shim uses `PROMPT_COMMAND` as an array, added in 5.1 | `bash --version` |
| **python3 3.9 or newer** | the tutor daemon | `python3 -V` |
| **`script`** (util-linux) | records the terminal so the tutor can read command output | `script --version` |
| coreutils, `tar`, `find`, `awk`, `sed` | the game and the installer | already present |

Optional:

| Optional | Gives you |
|---|---|
| `numpy` | RAG grounding in your machine's man pages (skipped silently if missing) |
| [Ollama](https://ollama.com) | a real local LLM instead of the built-in offline tutor |
| `tmux` | the alternative side-panel layout (`./play.sh --pane`) |
| `git` | records which tutor revision a session ran under |

On Debian/Ubuntu, the mandatory set is:

```sh
sudo apt install bash python3 util-linux git
```

> **macOS / BSD:** not supported as-is. `script` takes different arguments
> there, and the default `bash` is 3.2. You would need `brew install bash`
> plus a rewrite of the `script` invocation in `play.sh`.

---

## 2. Install

```sh
git clone https://github.com/jubnr/gameshell-tutor.git
cd gameshell-tutor
./play.sh
```

That is the entire setup. There is no package manager, no build step and no
dependency to download. The game itself is bundled in `game/`.

On first run `play.sh` does all of this for you:

1. copies the bundled GameShell archive to `.game/` (so the original stays
   pristine),
2. injects the tutor shim into that copy,
3. extracts every mission's goal text into your data directory — the game
   makes its own files unreadable while you play, so this has to happen up
   front,
4. works out which missions can be auto-detected,
5. launches the game with the tutor daemon running behind it.

To check the install without playing:

```sh
python3 test/test_llm_backends.py    # should print ALL BACKEND TESTS PASSED
python3 test/test_engine_policy.py   # should print ALL ENGINE POLICY TESTS PASSED
```

### Using your own GameShell

```sh
./play.sh ~/Documents/gameshell.sh     # a self-extracting archive
./play.sh ~/Documents/gameshell-game/  # or an already-extracted directory
```

`play.sh` also picks up `~/Documents/gameshell-game/` or the newest
`~/Documents/gameshell-*-save.sh` automatically, so an existing installation
keeps its progress.

The shim is **inert** unless `GSH_TUTOR=1` is exported, so launching the game
the normal way (`bash gameshell.sh`) behaves exactly as it always did.

The Game Master speaks French or English, following your `$LANG`.

---

## 3. Playing

You never need to learn a `gsh` command — the Game Master is the interface.
His messages are magenta, and he speaks line by line, with a
`[space to continue]` pause at each section of a briefing.

| command | what it does |
|---|---|
| `gm` | opens a dialogue prompt — then write freely |
| `gm <question>` | a quick one-line question |
| `gm hint` | graded help, never the answer first |
| `gm goal` | hear the current mission again |
| `gm commands` | this list |
| `gm check` | submit your answer / trigger the check (`gm check < file` too) |
| `gm index` | the list of missions, with your progress |
| `gm goto N` | jump to mission N |
| `gm raw` | the engine's original parchment, untouched |
| `gm reset` | reset the current mission |
| `gm exit` | save and quit |
| `gm persona <name>` | change the teaching style |

French aliases work too: `indice`, `mission`, `fini`, `parchemin`, `quitter`,
`commandes`.

### Winning a mission

**On 33 of the 45 missions, success is detected for you.** After each command
the shim quietly evaluates the mission's own check in a sandboxed subshell; as
soon as it passes, the real check runs: congratulations, treasure, and the next
mission's briefing all arrive together.

**The other twelve you submit yourself with `gm check`**, and the Game Master
tells you so when you get there. Ten of them end in a viva — the check asks
you something ("what is the secret key?") and the answer only exists in your
head. The other two wait on something a silent probe cannot wait for. If a
mission wants its answer piped in, `gm check < file` works.

The tutor never runs the real check on a guess. A failed `gsh check` makes the
engine re-run the mission's setup — regenerating the very secret you were about
to type — and records the mission as failed in a log that cannot be corrected.

---

## 4. Using a real LLM (optional)

Skip this section entirely and the game still works: the default backend is a
deterministic offline tutor with hand-written French and English replies.

Backends are chosen by `GSH_TUTOR_LLM_BACKEND`, else the `llm` key in
`~/.local/share/gameshell-tutor/config.json`, else `mock`.

### `mock` — the default

No network, no model, no setup. Complete and playable.

### `ollama` — a local model

```sh
ollama pull qwen2.5:7b-instruct-q4_K_M
./ollama/build.sh                       # builds the "shell-tutor" model
export GSH_TUTOR_LLM_BACKEND=ollama
./play.sh
```

The tutor's own system prompt is frozen in `ollama/Modelfile` — base model,
quantization and prompt all versioned with the repo. **Any edit to the
`Modelfile` needs `./ollama/build.sh` to take effect**, otherwise the running
model keeps its old prompt.

Replies are streamed, so the Game Master talks while he thinks.

*Grounding in your own man pages (optional):* with `numpy` installed, answers
can cite the documentation actually on this machine.

```sh
pip install numpy
ollama pull nomic-embed-text
python3 tutor/rag.py build       # ~445 fragments: man pages + mission texts
```

*Integrated GPU:* measured on a Radeon 780M, 9.3 → 16.6 tokens/s (1.8×).

```sh
sudo bash ollama/enable-igpu.sh          # Vulkan, then benchmark
sudo bash ollama/enable-igpu.sh --revert # back to CPU
```

The script reverts itself if the model stops answering, so it cannot leave you
with a broken tutor.

### `http` — any OpenAI-compatible endpoint

llama.cpp, vLLM, a gateway, anything speaking `/v1/chat/completions`:

```sh
export GSH_TUTOR_LLM_URL=http://localhost:8000   # or .../v1, or the full URL
export GSH_TUTOR_LLM_MODEL=my-model
export GSH_TUTOR_LLM_KEY=...                     # optional
```

No provider and no key is hardcoded. On any network failure the tutor falls
back to the offline mock automatically, so the game never breaks mid-session.

**Personas** (`gm persona <name>`): `socratic_diagnostician` (default),
`intent_scaffolder`, `apprentice_to_debug`, `postmortem_narrator`,
`adversary`. Same loop, different prompt, all bound by the same safety rules.

---

## 5. Running participants

`./subject.sh <id>` plays as a named subject and keeps everything they produce
in one directory. Two subjects never share state, and progress resumes across
sessions.

```sh
./subject.sh sub-01          # play as sub-01, created on first run
./subject.sh sub-01 --pane   # any play.sh flag is passed through
./subject.sh --list          # subjects, current mission, session count
./subject.sh --where sub-01  # print that subject's data directory
```

```
~/gameshell-subjects/sub-01/
  game.sh              their own copy of the game
  game-save.sh         written by the engine when they quit
  tutor/
    sessions/<stamp>/  one directory per session (see below)
    learner-<uid>.json what they demonstrated, per command
```

Each subject needs a whole copy of the game, not just a separate save:
GameShell keeps progress inside the archive, and the learner model is keyed on
the game's own `uid`. Sharing one game would merge both participants'
progress *and* their learner models.

Set `GSH_SUBJECTS` to move the root. Archiving or deleting a participant is one
`cp -r` or `rm -rf` of their directory.

### What a session records

```
sessions/20260807-143000/
  turns.jsonl      every command: text, exit code, cwd, mission, world snapshot
  chat.jsonl       every question the learner asked the Game Master
  tutor.jsonl      every reply he gave: kind, hint level, backend, latency, text
  condition.json   backend, model, persona, RAG state, tutor revision
  typescript       the raw terminal recording
  progress/        the engine's own missions.log and index.idx
  cursor.json      internal: how far the daemon has read
```

Both halves of the session are there, which is the point: `tutor.jsonl` makes
the hint level a **time series** rather than just a final number, and
`condition.json` records what the session actually ran with — something
`config.json` cannot tell you afterwards, because it can change between runs.

`progress/` matters because GameShell deletes the extracted game tree when you
quit; without it, mission names would be unrecoverable later.

Read a session back with:

```sh
python3 test/replay.py <session-dir>
```

It is strictly read-only — it never writes into the data it reads.

---

## 6. Where your data lives

Everything the tutor keeps is outside this repository, in
`~/.local/share/gameshell-tutor/`:

```
config.json        frontend, persona, llm backend
sessions/<stamp>/  one per game session
goals-cache/       mission goal texts + cached briefings
learner-<uid>.json what each player has demonstrated
no-probe.list      missions that cannot be auto-detected
noop-bin/          harmless stubs used while probing a mission's check
rag/               the embedding index, if you built one
```

Set `GSH_TUTOR_HOME` to move all of it. Deleting the directory resets the
tutor completely; the game's own progress lives in the game archive, not here.

---

## 7. Environment variables

| variable | effect |
|---|---|
| `GSH_TUTOR_HOME` | move all tutor state (default `~/.local/share/gameshell-tutor`) |
| `GSH_TUTOR_LLM_BACKEND` | `mock`, `ollama` or `http` |
| `GSH_TUTOR_LLM_URL` / `_MODEL` / `_KEY` | the `http` backend's endpoint |
| `GSH_TUTOR_OLLAMA_HOST` | point Ollama somewhere else, e.g. a GPU box |
| `GSH_TUTOR_PACE` | seconds per line; `0` prints everything at once |
| `GSH_TUTOR_ART` | `0` disables the ASCII scenery |
| `GSH_TUTOR_PROBE_TIMEOUT` | seconds a mission check may run before being killed (default 2) |
| `GSH_TUTOR_FRONTEND` | `pane` for the tmux layout |
| `GSH_SUBJECTS` | where `subject.sh` keeps participants |

---

## 8. Troubleshooting

**The Game Master never says anything.** The daemon did not start. Look at
`~/.local/share/gameshell-tutor/sessions/<newest>/daemon.log`. The usual cause
is `python3` missing from `PATH`. Typing `gm` restarts him.

**"The Game Master has gone quiet".** The daemon died mid-session. Type `gm`
to bring him back; he picks up where he left off rather than replaying the
whole session.

**Nothing is recorded / the tutor sees no output.** `script` is missing, or is
the BSD version. This project needs the util-linux one.

**The prompt looks odd or the shim does nothing.** Check `bash --version` is
5.1 or newer — the shim needs the array form of `PROMPT_COMMAND`.

**Briefings show the wrong text after you changed a template.** Cached
briefings are keyed by a fingerprint of the templates, so they normally retire
themselves; if in doubt, delete `goals-cache/narration-*.txt`.

**The local model advertises commands that no longer exist.** You edited
`ollama/Modelfile` without running `./ollama/build.sh`.

---

## 9. Development

```sh
python3 test/test_llm_backends.py     # LLM wiring + the hint-leak boundary
python3 test/test_engine_policy.py    # hint ladder, stuck detection, concepts
python3 test/replay.py <session-dir>  # inspect a past session (read-only)
```

The first two are plain scripts that exit non-zero on failure, so they slot
into any runner. There is no lint step and no build system.

`CLAUDE.md` documents the architecture and the invariants that are easy to
break — read it before changing the shim, the probe or the hint ladder.

**Generated quests:** `questgen/questgen.py` builds a complete mission in the
engine's own format from a JSON spec. The LLM invents the story and the success
condition but never writes shell; rendering goes through validated templates.

```sh
questgen/questgen.py --example --out /tmp/quest
./install.sh ~/Documents/gameshell-game --quest /tmp/quest
```

**Side panel** (the older layout): `./play.sh --pane` opens a tmux split with
the tutor on the right, using `/hint` and `/persona`.

---

## 10. How it teaches

- **Graded help**, per mission: 1 a question, 2 the concept, 3 a concrete
  lead, 4 the command. It only descends on real struggle, and it climbs back:
  a run of commands that work buys a rung back, and solving a mission resets
  it, so replaying one never starts at the answer. A command that fails
  without being a mistake — `grep` finding nothing, Ctrl-C — is not treated as
  struggle.
- **Diagnosis from the real error**: the shell's messages are quoted, not
  recalled from memory. The hint is added to that reading, never substituted
  for it, so you keep being told what the error means at the moment you are
  most stuck.
- **A word on every victory**, noting whether you were quick or had to
  persevere, and naming the commands you have now mastered. Everything he
  claims comes from what you actually did.
- **Idiomatic review** after a success: the shorter way you could have done
  it, with its tradeoffs.
- **A warning before the damage sticks**: a command that casts too wide a net
  (`rm *` in a cellar that also holds two protected bats) draws the mission's
  own caution and the way back — `gm reset`.

### The safety contract

- The tutor only ever receives **real data**: your command, its exact output,
  the exit code, the working directory, a bounded snapshot of the world, and
  the mission goal as *intent* — never the contents of the check.
- Success is decided by the engine, never by the LLM.
- Hints are cut at the network boundary: a remote model is sent only the rungs
  you have already unlocked, never the solution in advance.
- If output was not captured, the tutor says so instead of inventing it.
- You keep the keyboard. The tutor never types anything.

44 of the 45 missions have hand-written metadata (intent, a French/English
hint ladder, an idiomatic review) in `tutor/missions_meta/`. The 45th is the
closing screen.

---

## License

GPLv3, like GameShell (see `LICENSE`). The game bundled in `game/` is GameShell
itself (v0.6.0-39-g53f470d) by Pierre Hyvernat and contributors, also GPLv3:
see `game/README.md`.
