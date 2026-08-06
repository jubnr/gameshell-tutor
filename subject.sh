#!/usr/bin/env bash
# subject.sh — launch the game for ONE named subject, fully isolated. GPLv3.
#
# Usage:
#   ./subject.sh sub-01              # play as sub-01 (creates it on first run)
#   ./subject.sh sub-01 --pane       # any play.sh flag is passed through
#   ./subject.sh --list              # subjects and their progress
#   ./subject.sh --where sub-01      # print the subject's data directory
#
# Everything a subject produces lives under one directory, so archiving or
# deleting a participant is a single `cp -r` / `rm -rf`:
#
#   $GSH_SUBJECTS/sub-01/
#     game.sh              their own copy of the game (progress lives here)
#     game-save.sh         written by the engine when they quit
#     tutor/               GSH_TUTOR_HOME: sessions, learner model, config
#       sessions/<stamp>/  turns.jsonl, chat.jsonl, typescript, outbox
#       learner-<uid>.json what they have demonstrated, per command
#
# Why a whole game copy per subject: GameShell keeps progress inside the
# archive (it re-saves and deletes the extracted tree on exit), and the tutor
# keys the learner model on the game's own $GSH_CONFIG/uid. Sharing one game
# would merge two participants' progress AND their learner models.
#
# Override the root with GSH_SUBJECTS (default ~/gameshell-subjects).
# The RAG index is shared through a symlink when one already exists, since it
# is identical for everyone and expensive to rebuild.

set -e
HERE=$(cd "$(dirname "$0")"; pwd -P)
ROOT="${GSH_SUBJECTS:-$HOME/gameshell-subjects}"

die() { echo "error: $*" >&2; exit 1; }

list_subjects() {
  [ -d "$ROOT" ] || { echo "no subjects yet under $ROOT"; return 0; }
  printf '%-14s %-10s %-8s %s\n' SUBJECT MISSION SESSIONS "LAST PLAYED"
  local d name log mission sessions last
  for d in "$ROOT"/*/; do
    [ -d "$d" ] || continue
    name=$(basename "$d")
    mission="-"
    # the engine's own log is the source of truth for progress
    log=$(ls -1 "$d"/.game*/.config/missions.log "$d"/*/.config/missions.log 2>/dev/null | head -n1)
    [ -n "$log" ] && mission=$(awk '$2=="START"{m=$1} END{print (m==""?"-":m)}' "$log")
    sessions=$(ls -1d "$d/tutor/sessions"/*/ 2>/dev/null | wc -l)
    last=$(date -r "$d" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "-")
    printf '%-14s %-10s %-8s %s\n' "$name" "$mission" "$sessions" "$last"
  done
}

case ${1:-} in
  --list|-l) list_subjects; exit 0 ;;
  --where)   [ -n "${2:-}" ] || die "usage: $0 --where <subject>"
             echo "$ROOT/$2"; exit 0 ;;
  ""|-*)     die "usage: $0 <subject-id> [play.sh flags] | --list | --where <id>" ;;
esac

SUBJ=$1; shift
case $SUBJ in
  */*|.|..) die "subject id must be a plain name (got '$SUBJ')" ;;
esac

DIR="$ROOT/$SUBJ"
mkdir -p "$DIR/tutor"

# First run: give this subject their own copy of the bundled game.
if [ ! -e "$DIR/game.sh" ] && ! ls "$DIR"/*-save*.sh >/dev/null 2>&1; then
  [ -f "$HERE/game/gameshell.sh" ] || die "no bundled game at $HERE/game/gameshell.sh"
  cp "$HERE/game/gameshell.sh" "$DIR/game.sh"
  chmod +x "$DIR/game.sh"
  echo "[subject] created $SUBJ -> $DIR"
fi

# Resume from the newest savefile the engine wrote, else the pristine copy.
# (GameShell stores progress in the savefile: launching the original archive
# every time would restart the subject at mission 1.)
TARGET=$(ls -t "$DIR"/*-save*.sh 2>/dev/null | head -n1)
TARGET=${TARGET:-$DIR/game.sh}

# Share the RAG index: same for everyone, slow to rebuild, read-only at play.
SHARED_RAG="${GSH_TUTOR_HOME_SHARED:-$HOME/.local/share/gameshell-tutor}/rag"
if [ -d "$SHARED_RAG" ] && [ ! -e "$DIR/tutor/rag" ]; then
  ln -s "$SHARED_RAG" "$DIR/tutor/rag"
fi

echo "[subject] $SUBJ  game=$(basename "$TARGET")  data=$DIR"
export GSH_TUTOR_HOME="$DIR/tutor"
exec "$HERE/play.sh" "$@" "$TARGET"
