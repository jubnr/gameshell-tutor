#!/usr/bin/env bash
# install.sh — inject the tutor shim into a GameShell self-extracting archive
# (gameshell.sh or a gameshell-save-*.sh savefile) and/or a live extracted
# game directory, and pre-extract the mission goal texts the tutor needs at
# runtime (mission files are protected/unreadable while the game runs).
# GPLv3.
#
# Usage:
#   ./install.sh <archive.sh | extracted_game_dir> [--quest <mission_dir>]
#
# The shim is INERT unless GSH_TUTOR=1: normal play is unaffected.
# A .bak backup of the archive is created before patching.

set -e

HERE=$(cd "$(dirname "$0")"; pwd -P)
SHIM="$HERE/shim/gshrc_tutor.sh"
TUTOR_HOME="${GSH_TUTOR_HOME:-$HOME/.local/share/gameshell-tutor}"
GOALS_CACHE="$TUTOR_HOME/goals-cache"

TARGET=$1
QUEST=""
[ "$2" = "--quest" ] && QUEST=$3

if [ -z "$TARGET" ] || ! [ -e "$TARGET" ]; then
  echo "usage: $0 <gameshell archive .sh | extracted game dir> [--quest DIR]" >&2
  exit 1
fi

cache_goals() {
  # $1 = root containing missions/
  local missions="$1/missions" cached=0
  [ -d "$missions" ] || return 0
  mkdir -p "$GOALS_CACHE"
  local goal set_dir mission_dir name
  while IFS= read -r goal; do
    mission_dir=$(dirname "$(dirname "$goal")")
    name=${mission_dir#"$missions"/}
    name=${name//\//__}
    mkdir -p "$GOALS_CACHE/$name"
    cp -f "$goal" "$GOALS_CACHE/$name/"
    cached=$((cached+1))
  done < <(find "$missions" -path '*/goal/*.txt')
  echo "  goals cache: $cached goal files -> $GOALS_CACHE"
}

make_noop_bin() {
  # Stub commands used to neutralise a check.sh's cleanup while we probe it.
  # A shell function would not be enough: the checks do
  #     find ... -print0 | xargs -0 rm -f
  # and xargs execs `rm` itself, so the stub has to win on PATH, not in the
  # shell. `find` stays real, since checks legitimately use it to look around.
  # `kill` is stubbed too: intermediate/04_bg_xeyes ends both of its failure
  # branches with `ps -e | awk '/xeyes/ {print $1}' | xargs kill -9`, which
  # killed the very process the mission asks the learner to keep running.
  # xargs execs the binary, so the PATH stub catches that form; the shim also
  # defines a kill() function inside the probe, because `kill` is a builtin
  # too and a direct `kill -9 $pid` would never reach PATH.
  local dir="$TUTOR_HOME/noop-bin" c
  mkdir -p "$dir"
  for c in rm rmdir mv cp truncate shred dd kill pkill killall \
           chmod chown chgrp ln mkdir touch install; do
    printf '#!/bin/sh\n# no-op stub: see make_noop_bin in install.sh\nexit 0\n' \
      > "$dir/$c"
    chmod +x "$dir/$c"
  done
  echo "  no-op stubs -> $dir"
}

scan_unsafe_checks() {
  # $1 = root containing missions/
  #
  # The shim auto-detects success by sourcing a mission's own check.sh after
  # every command. A subshell keeps variables and $PWD from leaking, but it
  # does NOT stop filesystem writes -- and several GameShell checks CLEAN UP
  # on their failure path. basic/06_mv_coins_garden is the worst: a failed
  # check runs
  #     find "$GSH_HOME" -name "coin_?" -type f -print0 | xargs -0 rm -f
  # (the engine's own source even says "#FIXME: use clean.sh"), so probing it
  # after every command deleted the three coins the mission is about.
  #
  # This used to be a blacklist: only the checks a grep flagged as destructive
  # ran with no-op stubs on PATH. A blacklist over hand-written shell always
  # has a hole, and it had two big ones -- intermediate/04_bg_xeyes ends both
  # failure branches with `ps -e | awk … | xargs kill -9` (it killed the very
  # process the mission asks the learner to keep running), and
  # intermediate/05_background does `rm -f "$PIDS"` at the TOP of the check,
  # before any verdict. Neither mentions GSH_HOME, which the old rule required.
  #
  # The costs are wildly asymmetric: stubbing a harmless check leaks a temp
  # file, missing a harmful one destroys the learner's work. So we no longer
  # decide -- EVERY probe now runs sandboxed (see _tutor_probe in the shim).
  # The only thing that made blanket sandboxing expensive was the game's
  # `mktemp`, which writes inside the game tree and would get repacked into
  # every autosave; the shim points TMPDIR at the session directory instead.
  #
  # What still needs a list is the checks that must not be probed AT ALL.
  local missions="$1/missions" out="$TUTOR_HOME/no-probe.list" n=0
  [ -d "$missions" ] || return 0
  mkdir -p "$TUTOR_HOME"
  : > "$out"
  local f name why
  while IFS= read -r f; do
    why=""
    # (a) Checks that ASK the learner something ("What is the secret key?").
    # They `read` from stdin, so the stdin-closed probe can never pass them --
    # and permissions/03 does it in a `while true` loop, where read-at-EOF
    # spins forever. The answer only exists in the learner's head: the Game
    # Master nudges them toward `gm fini` instead.
    if grep -qE '(^|[;&|`$(]|[[:space:]])read[[:space:]]+(-[A-Za-z]+[[:space:]]+)*[A-Za-z_]' \
         "$f" 2>/dev/null; then
      why="interactive"
    # (b) Checks that wait. processes/03_pstree_kill busy-waits on a file with
    # no timeout of its own (`while ! [ -e "$GSH_TMP/snowflakes.list" ]`), and
    # the probe runs from PROMPT_COMMAND -- it hung the prompt outright.
    # intermediate/05_background sleeps 2s unconditionally, which would tax
    # every prompt. The shim's watchdog is the backstop; this is the cure.
    elif grep -vE '^[[:space:]]*#' "$f" 2>/dev/null \
         | grep -qE '(^|[[:space:]])(until|while[[:space:]]+!?[[:space:]]*\[)|[[:space:]]sleep[[:space:]]+[1-9]'; then
      why="slow"
    else
      continue
    fi
    name=$(dirname "$f"); name=${name#"$missions"/}
    printf '%s\n' "$name" >> "$out"
    n=$((n+1))
    [ -n "$VERBOSE" ] && echo "    no-probe ($why): $name"
  done < <(find "$missions" -name check.sh 2>/dev/null | sort)
  echo "  $n mission(s) never probed (learner submits with \`gm fini\`) -> $out"
  # every probe is sandboxed now: an old list would only mislead
  rm -f "$TUTOR_HOME/sandbox-check.list" "$TUTOR_HOME/interactive-check.list"
  # a stale no-autocheck.list from an older install would still disable them
  rm -f "$TUTOR_HOME/no-autocheck.list"
}

install_shim_into() {
  # $1 = root of a game tree.
  # Fresh games: a "!tutor_hook" pseudo-mission (pure mission-format data);
  # GameShell's init copies its gshrc into $GSH_CONFIG and re-copies it on
  # every new-game reset. NOTE: init wipes $GSH_CONFIG, so this is the only
  # path that survives a reset — never pre-create .config in a fresh archive
  # (start.sh would think a previous game exists).
  local root=$1
  mkdir -p "$root/missions/tutor_hook"
  cp -f "$SHIM" "$root/missions/tutor_hook/gshrc"
  local idx="$root/missions/default.idx"
  if [ -f "$idx" ] && ! grep -qx '!tutor_hook' "$idx"; then
    sed -i '1i !tutor_hook' "$idx"
    echo "  pseudo-mission '!tutor_hook' -> $idx"
  fi
  # Saved/continuing games skip init entirely, so also drop the shim
  # directly into an existing .config (both copies may load; the shim's
  # _TUTOR_ACTIVE guard makes the second a no-op).
  if [ -d "$root/.config" ]; then
    cp -f "$SHIM" "$root/.config/gshrc_tutor.sh"
    echo "  shim -> $root/.config/gshrc_tutor.sh (saved game)"
    # refresh any copy a previous game-init installed from tutor_hook —
    # it loads first alphabetically and would shadow the new one
    local old
    for old in "$root"/.config/gshrc_*tutor_hook.sh; do
      [ -e "$old" ] || break
      cp -f "$SHIM" "$old"
      echo "  shim -> $old (refreshed stale copy)"
    done
  fi
}

install_quest_into() {
  # $1 = root of game tree; installs $QUEST as missions/<name> + index entries
  local root=$1 name
  name=$(basename "$QUEST")
  cp -r "$QUEST" "$root/missions/$name"
  for idx in "$root/missions/default.idx" "$root/.config/index.idx"; do
    if [ -f "$idx" ] && ! grep -qx "$name" "$idx"; then
      # insert before FINAL_MISSION if present, else append
      if grep -qx "FINAL_MISSION" "$idx"; then
        sed -i "s/^FINAL_MISSION$/$name\nFINAL_MISSION/" "$idx"
      else
        echo "$name" >> "$idx"
      fi
      echo "  quest '$name' added to $idx"
    fi
  done
}

if [ -d "$TARGET" ]; then
  # ---- live/extracted game directory -------------------------------------
  ROOT=$(cd "$TARGET"; pwd -P)
  if ! [ -f "$ROOT/start.sh" ]; then
    echo "error: $ROOT does not look like a GameShell tree (no start.sh)" >&2
    exit 1
  fi
  install_shim_into "$ROOT"
  cache_goals "$ROOT"
  make_noop_bin
  scan_unsafe_checks "$ROOT"
  [ -n "$QUEST" ] && install_quest_into "$ROOT"
else
  # ---- self-extracting archive -------------------------------------------
  MARK_LINE=$(awk '/^##START_OF_GAMESHELL_ARCHIVE##/ {print NR; exit}' "$TARGET")
  if [ -z "$MARK_LINE" ]; then
    echo "error: $TARGET is not a GameShell self-extracting archive" >&2
    exit 1
  fi
  WORK=$(mktemp -d)
  trap 'rm -rf "$WORK"' EXIT
  tail -n+"$((MARK_LINE + 1))" "$TARGET" > "$WORK/game.tgz"
  mkdir "$WORK/tree"
  tar -zxf "$WORK/game.tgz" -C "$WORK/tree"
  # autosaves taken mid-session carry the anti-cheat chmod state (dirs 0311,
  # unlistable) — restore owner read on the temp tree so we can patch it;
  # the game re-applies protection at every launch anyway
  chmod -R u+rwX "$WORK/tree"
  TOP=$(find "$WORK/tree" -mindepth 1 -maxdepth 1 -type d | head -n1)
  if [ -z "$TOP" ]; then
    echo "error: unexpected archive layout" >&2
    exit 1
  fi
  install_shim_into "$TOP"
  cache_goals "$TOP"
  make_noop_bin
  scan_unsafe_checks "$TOP"
  [ -n "$QUEST" ] && install_quest_into "$TOP"

  head -n "$MARK_LINE" "$TARGET" > "$WORK/new.sh"
  tar -zcf - -C "$WORK/tree" "$(basename "$TOP")" >> "$WORK/new.sh"

  # safety: the patched archive must contain at least as many entries as the
  # original (tar -c silently skips unreadable dirs; that once truncated a
  # protected autosave). Abort without touching the target if it shrank.
  OLD_N=$(tail -n+"$((MARK_LINE + 1))" "$TARGET" | tar -tzf - 2>/dev/null | wc -l)
  NEW_N=$(tail -n+"$((MARK_LINE + 1))" "$WORK/new.sh" | tar -tzf - 2>/dev/null | wc -l)
  if [ "$NEW_N" -lt "$OLD_N" ]; then
    echo "error: patched archive would lose content ($OLD_N -> $NEW_N entries); aborting, target untouched" >&2
    exit 1
  fi

  # only create the backup once — never clobber an existing one with a
  # possibly already-patched target
  [ -e "$TARGET.bak" ] || cp -f "$TARGET" "$TARGET.bak"
  mv -f "$WORK/new.sh" "$TARGET"
  chmod +x "$TARGET"
  echo "  archive patched in place ($OLD_N -> $NEW_N entries, backup: $TARGET.bak)"
fi

echo "done. Launch with: $HERE/play.sh [$TARGET]"
