#!/usr/bin/env bash
# play.sh — launch GameShell with the tutor. GPLv3.
#
# Usage: ./play.sh [--pane] [archive.sh | extracted_game_dir]
#
# Default (in-game): ONE window — the game runs here, and "le Maître du Jeu"
# lives inside it: his comments appear at the prompt, and you talk to him
# with `gm` (gm <question> | gm indice | gm persona <nom>).
# A background daemon (auto-started by the shim) does the
# tutoring; script(1) records exact output as before.
#
# --pane (or config frontend="terminal_pane"): the previous layout — tmux
# split with the tutor chat in a right-hand pane.
#
# Default target: newest gameshell*-save*.sh next to ~/Documents/gameshell.sh,
# else ~/Documents/gameshell.sh itself.

set -e
HERE=$(cd "$(dirname "$0")"; pwd -P)
TUTOR_HOME="${GSH_TUTOR_HOME:-$HOME/.local/share/gameshell-tutor}"

FRONTEND=$(python3 -c "
import json
try: print(json.load(open('$TUTOR_HOME/config.json')).get('frontend',''))
except Exception: print('')" 2>/dev/null)
[ "$FRONTEND" = "terminal_pane" ] && PANE=1 || PANE=""
if [ "$1" = "--pane" ]; then PANE=1; shift; fi

TARGET=${1:-}
if [ -z "$TARGET" ]; then
  # preferred: the PERSISTENT game dir (~2-3s launch, no repack, no
  # savefile-staleness); archives remain supported as explicit targets
  if [ -d "$HOME/Documents/gameshell-game" ]; then
    TARGET=$HOME/Documents/gameshell-game
  elif ls "$HOME"/Documents/gameshell*-save*.sh >/dev/null 2>&1; then
    TARGET=$(ls -t "$HOME"/Documents/gameshell*-save*.sh | head -n1)
  elif [ -f "$HOME/Documents/gameshell.sh" ]; then
    TARGET=$HOME/Documents/gameshell.sh
  elif [ -f "$HERE/game/gameshell.sh" ]; then
    # nothing installed on this machine: fall back to the archive bundled in
    # this repo, so a fresh `git clone && ./play.sh` just works. Play from a
    # COPY under .game/ (gitignored): install.sh patches the archive in place
    # and autosaves rewrite it, neither of which should touch a tracked file.
    mkdir -p "$HERE/.game"
    [ -f "$HERE/.game/gameshell.sh" ] || cp "$HERE/game/gameshell.sh" "$HERE/.game/gameshell.sh"
    chmod +x "$HERE/.game/gameshell.sh"
    TARGET="$HERE/.game/gameshell.sh"
  fi
fi
if ! [ -e "$TARGET" ]; then
  echo "error: no GameShell archive/dir found ($TARGET)" >&2
  exit 1
fi

# a session killed without cleanup (closed terminal…) leaves the anti-cheat
# chmod state behind; restore owner access before launching — the game
# re-applies protection at every session start anyway
if [ -d "$TARGET" ]; then
  chmod u+rwX "$TARGET" "$TARGET/missions" "$TARGET/.config" \
    "$TARGET/.tmp" "$TARGET/.sbin" 2>/dev/null || true
fi

# keep the embedded shim fresh: the game AUTOSAVES on every mission passed,
# overwriting the savefile with whatever shim version the running session was
# launched with — so refresh at every launch (idempotent, ~2s) unless the
# embedded copy already matches shim/gshrc_tutor.sh
NEED_INSTALL=1
if [ -f "$TARGET" ]; then
  MARK=$(awk '/^##START_OF_GAMESHELL_ARCHIVE##/ {print NR; exit}' "$TARGET")
  if [ -n "$MARK" ] && tail -n+"$((MARK + 1))" "$TARGET" \
      | tar -xzOf - --wildcards '*/missions/tutor_hook/gshrc' 2>/dev/null \
      | cmp -s - "$HERE/shim/gshrc_tutor.sh"; then
    NEED_INSTALL=0
  fi
elif cmp -s "$TARGET/missions/tutor_hook/gshrc" "$HERE/shim/gshrc_tutor.sh" \
    2>/dev/null; then
  # the init-installed .config copy loads FIRST (continuing games skip
  # init): it must match too, or a stale version wins via _TUTOR_ACTIVE
  NEED_INSTALL=0
  for _c in "$TARGET"/.config/gshrc_*tutor_hook.sh; do
    [ -e "$_c" ] || break
    cmp -s "$_c" "$HERE/shim/gshrc_tutor.sh" || NEED_INSTALL=1
  done
fi
# The bundled archive already carries the shim, so the check above would skip
# install.sh on a fresh clone -- and the goal texts would never be extracted,
# leaving every briefing empty. install.sh also writes the no-op stubs and the
# sandbox list, so run it whenever this machine has no runtime state yet.
if [ ! -d "$TUTOR_HOME/goals-cache" ] || [ -z "$(ls -A "$TUTOR_HOME/goals-cache" 2>/dev/null)" ] \
   || [ ! -d "$TUTOR_HOME/noop-bin" ]; then
  NEED_INSTALL=1
fi
if [ "$NEED_INSTALL" = 1 ]; then
  echo "[tuteur] mise à jour du shim dans $TARGET…"
  "$HERE/install.sh" "$TARGET" >/dev/null
fi

SESS="$TUTOR_HOME/sessions/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$SESS"
printf '%s\n' "$SESS" > "$TUTOR_HOME/current-session"

if [ -d "$TARGET" ]; then
  # persistent dir: -C = continue without the "restart? [y/N]" prompt
  # (same flag the self-extracting header passes). Fresh dir (no .config):
  # no flag, init runs normally.
  if [ -d "$TARGET/.config" ]; then
    GAME_CMD="bash \"$TARGET/start.sh\" -C"
  else
    GAME_CMD="bash \"$TARGET/start.sh\""
  fi
  # GSH_EXEC_DIR/GSH_EXEC_FILE are exported by lib/header.sh, the
  # self-extracting archive's preamble. Running start.sh straight out of a
  # directory skips header.sh entirely, so the game's autosave (GSH_AUTOSAVE=1,
  # GSH_SAVEFILE_MODE=simple) builds its path out of two empty variables:
  #   "$GSH_EXEC_DIR/${GSH_EXEC_FILE%.*}-save.${GSH_EXEC_FILE##*.}"  ->  /-save.
  # which fails with "Permission denied" at the filesystem root after EVERY
  # passed mission, and prints SAVEFILE MIGHT BE INCORRECT. Supply them here.
  # The "-game" suffix is play.sh's own convention for the extracted dir, so
  # gameshell-game/ saves as gameshell-save.sh, next to the archive it came
  # from and matching the gameshell*-save*.sh glob used above.
  : "${GSH_EXEC_DIR:=$(cd "$(dirname "$TARGET")"; pwd -P)}"
  : "${GSH_EXEC_FILE:=$(basename "${TARGET%-game}").sh}"
  export GSH_EXEC_DIR GSH_EXEC_FILE
else
  GAME_CMD="bash \"$TARGET\""
fi
# tiny runner so env + quoting survive tmux/script regardless of server env
cat > "$SESS/run-game.sh" <<EOF
export GSH_TUTOR=1
export GSH_TUTOR_SESSION="$SESS"
export GSH_TUTOR_HOME="$TUTOR_HOME"
export GSH_TUTOR_ROOT="$HERE"
${GSH_EXEC_DIR:+export GSH_EXEC_DIR=$(printf %q "$GSH_EXEC_DIR")}
${GSH_EXEC_FILE:+export GSH_EXEC_FILE=$(printf %q "$GSH_EXEC_FILE")}
${PANE:+export GSH_TUTOR_FRONTEND=pane}
exec script -qf "$SESS/typescript" -c '$GAME_CMD'
EOF

if [ -n "$PANE" ]; then
  GAME="bash '$SESS/run-game.sh'"
  PANE_CMD="python3 '$HERE/tutor/tutor_pane.py' '$SESS'"
  if command -v tmux >/dev/null; then
    if [ -n "$TMUX" ]; then
      tmux split-window -h "$PANE_CMD"
      tmux select-pane -L
      eval "$GAME"
    else
      tmux new-session "$GAME" \; split-window -h "$PANE_CMD" \; select-pane -L
    fi
  else
    echo ">>> no tmux: run this in another terminal for the tutor pane:"
    echo ">>>   $PANE_CMD"
    eval "$GAME"
  fi
else
  # in-game Maître du Jeu: single window, daemon auto-started by the shim
  rc=0
  bash "$SESS/run-game.sh" || rc=$?
  # nudge the daemon in case the watchdog hasn't noticed the exit yet
  kill "$(cat "$SESS/daemon.pid" 2>/dev/null)" 2>/dev/null || true
  exit "$rc"
fi
