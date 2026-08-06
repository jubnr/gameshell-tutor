# gameshell-tutor

An LLM tutor that lives **inside** your GameShell, playing the part of **the
Game Master** 🧙. It comments on your mistakes, narrates each mission and
answers your questions, without ever typing a command for you.

The whole idea in one line: **the shell executes and judges, the LLM only
interprets**. The GameShell engine itself is never modified.

```
[mission 5] $ rm spider_1
rm: cannot remove 'spider_1': Permission denied
🧙 The Game Master (about `rm spider_1`)
   "Permission denied" is a rights problem, not a missing file.
   What does `ls -l` say about it?
[mission 5] $ gm hint
```

## Install

Requirements: `bash` and `python3`. Nothing else: the game itself is bundled,
and the tutor runs fully offline by default, with no LLM.

```sh
git clone <this-repo> && cd gameshell-tutor
./play.sh
```

That is the whole setup. `play.sh` finds the GameShell archive in `game/`,
copies it to `.game/`, injects the shim, extracts the mission goal texts, and
launches: one window, the Game Master inside it, the tutor daemon in the
background.

Already have your own GameShell? Pass it and it is used instead:

```sh
./play.sh ~/Documents/gameshell.sh     # or an extracted game directory
```

`play.sh` also picks up `~/Documents/gameshell-game/` or a
`~/Documents/gameshell-*-save.sh` automatically, so an existing installation
keeps its progress.

The shim is **inert** unless `GSH_TUTOR=1` is exported, so launching the game
normally (`bash gameshell.sh`) behaves exactly as before.

The Game Master speaks French or English, following your `$LANG`.

## Talking to the Game Master

You never need to learn a `gsh` command, he is the interface. His messages are
magenta, and he speaks line by line with a `[space to continue]` pause at each
section of a briefing (`GSH_TUTOR_PACE=0` prints everything at once).

| command | effect |
|---|---|
| `gm` | opens a dialogue prompt, then write freely |
| `gm <question>` | quick one-line question |
| `gm hint` | graded help, never the answer first |
| `gm goal` | hear the current mission again |
| `gm commands` | this list |
| `gm check` | trigger the mission check yourself |
| `gm index` / `gm goto N` | list the missions, jump to N |
| `gm raw` | the engine's original parchment, untouched |
| `gm reset` / `gm exit` | reset the mission, save and quit |
| `gm persona <name>` | change the teaching style |

French aliases work too: `indice`, `mission`, `fini`, `parchemin`, `quitter`,
`commandes`.

**Success is detected automatically.** After each command the shim silently
evaluates the mission's own `check.sh`; if it passes, it triggers the engine's
real check: congratulations, treasure, next mission and its briefing in one
go. Interactive checks (the merchant asking a question) cannot pass silently,
so run `gm check` for those.

Every mission family opens with its own ASCII scene (the keep, the maze, the
merchant's stall, the potion lab, and so on) above the briefing.
`GSH_TUTOR_ART=0` disables them.

## The safety contract

- The tutor only ever receives **real data**: the command you typed, its exact
  output, the exit code, the working directory, a bounded snapshot of the
  world, and the mission goal as INTENT (never the contents of `check.sh`).
- Success is decided by the engine's `gsh check`, never by the LLM.
- If output was not captured, the tutor says so and asks you to run the
  command, instead of inventing it.
- You keep the keyboard: the tutor never types anything.

## How it teaches

- **Graded help**, stored per mission: 1 a question, 2 the concept, 3 a
  concrete lead, 4 the command. It only steps down on real struggle (repeated
  failures, the same error again and again, long inactivity). The LLM is sent
  only the rungs already unlocked, never the solution in advance.
- **Diagnosis from the real error**: the shell's messages are quoted, not
  paraphrased from memory.
- **Idiomatic review** after a success: the tutor rereads the commands you
  actually used and offers the shorter version, with its tradeoffs.
- **Never re-explain a mastered concept**: every command carries a
  seen / used / mastered status in the learner model.

44 of the 45 missions have hand-written metadata (intent, fr/en hint ladder,
idiomatic review) in `tutor/missions_meta/`. The 45th is the closing screen.

## Choosing an LLM

Three backends. Selected by `GSH_TUTOR_LLM_BACKEND`, else the `llm` key in
`~/.local/share/gameshell-tutor/config.json`, else `mock`.

**`mock`** (default): deterministic hand-written replies, fully offline,
French and English. The game is complete without any LLM at all.

**`ollama`**: a local model, streamed, so the Game Master talks while he
generates. The tutor is frozen in `ollama/Modelfile`: base model, quantization
and system prompt all versioned with the repo.

```sh
ollama pull qwen2.5:7b-instruct-q4_K_M
./ollama/build.sh                      # creates the "shell-tutor" model
export GSH_TUTOR_LLM_BACKEND=ollama
./play.sh
```

Any edit to the `Modelfile` needs `./ollama/build.sh` to take effect,
otherwise the running model keeps its old prompt.

*Local RAG*: answers are grounded in **your machine's man pages** plus the
mission texts (445 fragments, `nomic-embed-text` embeddings). Rebuild after
adding missions: `python3 tutor/rag.py build`.

*Integrated GPU*: measured on a Radeon 780M, 9.3 to 16.6 tokens/s (1.8x).

```sh
sudo bash ollama/enable-igpu.sh          # Vulkan, then benchmark
sudo bash ollama/enable-igpu.sh --revert # back to CPU
```

The script reverts itself if the model stops answering, so it cannot leave
you with a broken tutor.

**`http`**: any OpenAI-compatible endpoint (llama.cpp, vLLM, gateways).

```sh
export GSH_TUTOR_LLM_URL=http://localhost:8000   # or .../v1, or the full URL
export GSH_TUTOR_LLM_MODEL=my-model
export GSH_TUTOR_LLM_KEY=...                     # optional
```

No provider or key is hardcoded. On a network failure it falls back to the
mock automatically, so the game never breaks.

Personas (`gm persona <name>`): `socratic_diagnostician` (default),
`intent_scaffolder`, `apprentice_to_debug`, `postmortem_narrator`,
`adversary`. Same loop, different prompt, all bound by the safety contract.

## Extras

**Generated quests**: `questgen/questgen.py` builds a complete mission in the
engine's own format from a JSON spec. The LLM invents the story and the
predicate but never writes shell, rendering goes through validated templates.

```sh
questgen/questgen.py --example --out /tmp/quest          # offline
./install.sh ~/Documents/gameshell-game --quest /tmp/quest
```

**Tests**, without launching the game:

```sh
python3 test/test_llm_backends.py            # LLM wiring, fake local server
python3 test/replay.py <session-dir>         # replay a session on the mock
```

**Side panel** (the older layout): `./play.sh --pane` opens a tmux split with
the tutor on the right, using `/hint` and `/persona`.

## License

GPLv3, like GameShell (see `LICENSE`). The game bundled in `game/` is
GameShell itself (v0.6.0-39-g53f470d), by Pierre Hyvernat and contributors,
also GPLv3: see `game/README.md`.
