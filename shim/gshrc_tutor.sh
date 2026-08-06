# gshrc_tutor.sh — GameShell Tutor SessionBridge (shell side). GPLv3.
#
# Installed two ways (same file, no engine edit in either case):
#  - as missions/tutor_hook/gshrc + a "!tutor_hook" line in default.idx:
#    GameShell's own init copies it to $GSH_CONFIG/gshrc_0001-…_tutor_hook.sh
#    (survives new-game resets);
#  - copied directly to $GSH_CONFIG/gshrc_tutor.sh in savefiles/live dirs
#    (init is skipped when continuing a game).
# Both copies may be sourced in one session; the _TUTOR_ACTIVE guard makes
# the second one a no-op.
#
# INERT unless GSH_TUTOR=1 is exported before launching the game (play.sh
# does that): normal play is completely unaffected.
#
# When active (bash only) it records every learner command + exit code +
# cwd + mission + a bounded filesystem snapshot to $SESSION/turns.jsonl,
# and emits invisible OSC markers so the script(1) typescript can be sliced
# into exact per-command output by tutor/bridge.py. It never blocks, never
# calls any LLM, and never executes anything on the learner's behalf.
#
# NOTE: no top-level `return` here — when installed via missions/tutor_hook,
# GameShell appends TEXTDOMAIN-restore lines that must always run.

if [ "$GSH_TUTOR" = "1" ] && [ -n "$BASH_VERSION" ] && [ -z "$_TUTOR_ACTIVE" ]
then

_TUTOR_HOME="${GSH_TUTOR_HOME:-${REAL_HOME:-$HOME}/.local/share/gameshell-tutor}"
_TUTOR_SESS="${GSH_TUTOR_SESSION:-$_TUTOR_HOME/sessions/manual-$$}"

if mkdir -p "$_TUTOR_SESS/outbox" 2>/dev/null
then
  _TUTOR_ACTIVE=1
  _TUTOR_LOG="$_TUTOR_SESS/turns.jsonl"
  _TUTOR_OUT="$_TUTOR_SESS/outbox"
  _TUTOR_CHAT="$_TUTOR_SESS/chat.jsonl"
  _TUTOR_ID=0
  _TUTOR_GMID=0
  _TUTOR_PENDING=""
  _TUTOR_LASTCMD=""
  _TUTOR_EXPECT=1   # wait briefly at the FIRST prompt: the mission briefing
                    # is being prepared by the freshly spawned daemon
  _TUTOR_IN_FLUSH=""
  _TUTOR_AT_PROMPT=1

  # Missions whose check.sh cleans up the player's world on its failure path
  # (see scan_unsafe_checks in install.sh). Their cleanup runs AFTER the
  # verdict, so probing them stays accurate as long as it cannot write: we
  # put no-op rm/mv/cp stubs first on PATH for those, and keep auto-detection
  # everywhere. Loaded once: the list is static for the session.
  # (guard the redirect: a missing file makes bash print to the learner's
  # terminal, and 2>/dev/null on `done` does not suppress that)
  _TUTOR_SANDBOX="|"
  _TUTOR_NOOPBIN="$_TUTOR_HOME/noop-bin"
  if [ -r "$_TUTOR_HOME/sandbox-check.list" ]; then
    while IFS= read -r _m; do
      [ -n "$_m" ] && _TUTOR_SANDBOX="$_TUTOR_SANDBOX$_m|"
    done < "$_TUTOR_HOME/sandbox-check.list"
  fi

  # Missions whose check ASKS the learner something ("What is the secret
  # key?"). It reads stdin, so the silent probe (stdin closed) can never pass
  # it: the answer only exists in the learner's head. The Game Master launches
  # the real check for them instead, so no command has to be memorised.
  _TUTOR_INTERACTIVE="|"
  if [ -r "$_TUTOR_HOME/interactive-check.list" ]; then
    while IFS= read -r _m; do
      [ -n "$_m" ] && _TUTOR_INTERACTIVE="$_TUTOR_INTERACTIVE$_m|"
    done < "$_TUTOR_HOME/interactive-check.list"
  fi
  unset _m
  _TUTOR_MNB=""      # mission the command counter below belongs to
  _TUTOR_MCMDS=0     # real commands the learner ran in that mission

  _tutor_esc() {
    # minimal JSON string escaping
    local s=$1
    s=${s//\\/\\\\}; s=${s//\"/\\\"}
    s=${s//$'\n'/\\n}; s=${s//$'\r'/\\r}; s=${s//$'\t'/\\t}
    printf '%s' "$s"
  }

  _tutor_snapshot() {
    # bounded view of the play area: perms, sizes, paths
    find "$GSH_HOME" -maxdepth 4 -printf '%M %s %p\n' 2>/dev/null | head -n 120
  }

  _tutor_preexec() {
    # DEBUG trap body. Fires before every simple command; we only want the
    # first one of an interactive command line, from the main shell.
    [ -n "$COMP_LINE" ] && return                 # programmable completion
    [ "$BASHPID" != "$$" ] && return              # $(...) in PS1 => subshell
    [ "$_TUTOR_AT_PROMPT" != 1 ] && return        # already inside a line
    case $BASH_COMMAND in
      _tutor_*|_check_pwd*|_help_hint*) return ;;
      gm|gm\ *|maitre|maitre\ *)
        # talking to the Game Master is not shell activity: record no turn
        # (and mute the DEBUG trap for the function's internals)
        _TUTOR_AT_PROMPT=0
        return ;;
    esac
    _TUTOR_AT_PROMPT=0
    local cmd
    cmd=$(HISTTIMEFORMAT= builtin history 1)
    cmd=${cmd#"${cmd%%[![:space:]]*}"}            # strip leading spaces
    cmd=${cmd#* }                                 # strip history number
    cmd=${cmd#"${cmd%%[![:space:]]*}"}
    case $cmd in '#'*) return ;; esac             # history seed/comments
    _TUTOR_LASTCMD=$cmd
    _TUTOR_ID=$((_TUTOR_ID + 1))
    _TUTOR_PENDING=$_TUTOR_ID
    # invisible marker for typescript slicing
    printf '\033]777;gshtutor;pre;%s\007' "$_TUTOR_ID"
    printf '{"event":"pre","id":%s,"ts":%s,"cmd":"%s","cwd":"%s"}\n' \
      "$_TUTOR_ID" "$(date +%s)" "$(_tutor_esc "$cmd")" "$(_tutor_esc "$PWD")" \
      >> "$_TUTOR_LOG"
  }

  _tutor_postexec() {
    # MUST stay the first element of PROMPT_COMMAND to see the real $?
    local ec=$?
    [ -n "$_TUTOR_PENDING" ] || return 0
    printf '\033]777;gshtutor;post;%s;%s\007' "$_TUTOR_PENDING" "$ec"
    printf '{"event":"post","id":%s,"ts":%s,"exit":%s,"mission":"%s","cwd":"%s","snapshot":"%s"}\n' \
      "$_TUTOR_PENDING" "$(date +%s)" "$ec" "$(gsh pcm 2>/dev/null)" \
      "$(_tutor_esc "$PWD")" "$(_tutor_esc "$(_tutor_snapshot)")" \
      >> "$_TUTOR_LOG"
    _TUTOR_PENDING=""
    # after a failure or a `gsh check`, coaching is imminent and worth a
    # short bounded wait so it lands on THIS prompt (instant with the mock)
    if [ "$ec" != 0 ] ; then
      _TUTOR_EXPECT=1
    else
      case $_TUTOR_LASTCMD in "gsh check"*) _TUTOR_EXPECT=1 ;; esac
    fi
    # automatic success detection: silently evaluate the mission's own
    # check predicate; if it passes, run the REAL engine check
    case $_TUTOR_LASTCMD in
      gsh*|gm|gm\ *|maitre*|"") ;;
      *) _tutor_autocheck ;;
    esac
    return 0
  }

  # THE SHELL STILL JUDGES: this only evaluates the mission's own check.sh,
  # silently and side-effect-free (subshell, no stdin/stdout — some checks
  # `cd` or `read` on their failure path), and on success triggers the real
  # engine check with all its official consequences. The LLM decides nothing.
  _tutor_autocheck() {
    command -v mission_source >/dev/null 2>&1 || return 0
    local nb dir
    nb=$(gsh pcm 2>/dev/null)
    [ -n "$nb" ] || return 0
    dir=$(missiondir "$nb" 2>/dev/null)
    [ -n "$dir" ] && [ -f "$dir/check.sh" ] || return 0

    # count real commands within the current mission (reset when it changes)
    if [ "$nb" != "$_TUTOR_MNB" ]; then _TUTOR_MNB=$nb; _TUTOR_MCMDS=0; fi
    _TUTOR_MCMDS=$((_TUTOR_MCMDS + 1))

    # An interactive check cannot be probed, so run the real one: once the
    # learner has done something (1st command), then every 3rd after that
    # until they win. A wrong answer costs nothing here, the check simply
    # fails and comes round again.
    case "$_TUTOR_INTERACTIVE" in
      *"|${dir#*/missions/}|"*)
        if [ $((_TUTOR_MCMDS % 3)) = 1 ]; then
          echo
          _tutor_run_check
        fi
        return 0 ;;
    esac

    # a subshell contains variables and $PWD, NOT filesystem writes. For the
    # checks that tidy the world away when they fail, shadow the destructive
    # commands on PATH so the probe keeps its verdict and loses its bite.
    case "$_TUTOR_SANDBOX" in
      *"|${dir#*/missions/}|"*)
        ( PATH="$_TUTOR_NOOPBIN:$PATH"; mission_source "$dir/check.sh" ) \
          </dev/null >/dev/null 2>&1 || return 0 ;;
      *)
        ( mission_source "$dir/check.sh" ) </dev/null >/dev/null 2>&1 || return 0 ;;
    esac
    echo
    _tutor_run_check
    return 0
  }

  # run the real `gsh check` wrapped in a synthetic recorded turn (markers +
  # events) so the daemon sees it exactly like a typed check. The mission
  # number is captured BEFORE: on success the engine advances immediately.
  _tutor_run_check() {
    local nb ec
    nb=$(gsh pcm 2>/dev/null)
    _TUTOR_ID=$((_TUTOR_ID + 1))
    printf '\033]777;gshtutor;pre;%s\007' "$_TUTOR_ID"
    printf '{"event":"pre","id":%s,"ts":%s,"cmd":"gsh check","cwd":"%s"}\n' \
      "$_TUTOR_ID" "$(date +%s)" "$(_tutor_esc "$PWD")" >> "$_TUTOR_LOG"
    gsh check
    ec=$?
    printf '\033]777;gshtutor;post;%s;%s\007' "$_TUTOR_ID" "$ec"
    printf '{"event":"post","id":%s,"ts":%s,"exit":%s,"mission":"%s","cwd":"%s","snapshot":"%s"}\n' \
      "$_TUTOR_ID" "$(date +%s)" "$ec" "$(_tutor_esc "$nb")" \
      "$(_tutor_esc "$PWD")" "$(_tutor_esc "$(_tutor_snapshot)")" \
      >> "$_TUTOR_LOG"
    _TUTOR_EXPECT=1
    return "$ec"
  }

  # print pending Game Master messages (pre-rendered by the daemon, one file
  # per message, lexicographic order = delivery order; rename() on the daemon
  # side makes every listed file complete — no locking needed).
  # NOTE: the game's PATH wraps rm with safe_rm, which refuses to touch files
  # outside the GameShell tree — our spool is outside it by design. `command
  # -p rm` uses the default system PATH; if even that fails, truncating with
  # a builtin marks the file delivered (empty = consumed; the daemon janitors
  # empty files away).
  _tutor_deliver() {
    local f line delivered="" pace="${GSH_TUTOR_PACE:-0.05}"
    _TUTOR_DELIVERED=""
    for f in "$_TUTOR_OUT"/*.msg; do
      [ -s "$f" ] || continue
      [ -n "$delivered" ] || [ -n "$_TUTOR_STREAMING" ] || echo
      delivered=1
      _TUTOR_DELIVERED=1
      # move before printing: a Ctrl-C mid-speech loses the tail of the
      # message instead of replaying it at the next prompt
      if command -p mv -f "$f" "$f.r" 2>/dev/null; then f="$f.r"; fi
      if [ "$pace" = 0 ]; then
        # instant mode: drop the page-break markers, print everything
        tr -d '\006' < "$f"
      else
        # the Game Master speaks line by line, with a breath at paragraphs,
        # and waits for the learner at page breaks (\x06), RPG-style
        while IFS= read -r line || [ -n "$line" ]; do
          case $line in
            *$'\006'*)
              if [ "${LANG%%_*}" = fr ]; then
                printf '\033[2m   [espace pour continuer]\033[0m' >&2
              else
                printf '\033[2m   [space to continue]\033[0m' >&2
              fi
              read -rsn1 -t 60 line < /dev/tty 2>/dev/null
              printf '\r\033[K' >&2
              continue
              ;;
          esac
          printf '%s\n' "$line"
          if [ -z "${line//[[:space:]]/}" ]; then
            sleep 0.22
          else
            sleep "$pace"
          fi
        done < "$f"
      fi
      command -p rm -f "$f" 2>/dev/null || : > "$f"
    done
    return 0
  }

  # PROMPT_COMMAND element, right after _tutor_postexec: deliver queued
  # messages just before the prompt is drawn (never mid-typing, never inside
  # a command — full-screen apps and `read`-based checks stay safe).
  # The prompt is held back while the Game Master is preparing an answer:
  # after an error/check (_TUTOR_EXPECT, ~2s grace for the daemon to react)
  # and as long as the daemon's `pending` marker exists (LLM thinking),
  # hard-capped at 20s. A learner waiting for guidance must never face a
  # silent prompt while the answer is seconds away.
  _tutor_deliver_hook() {
    local i=0 spoke="" hinted=""
    # deliver-as-they-arrive loop: streamed chunks print progressively while
    # the daemon's `pending` marker says an answer is still being generated
    while :; do
      _TUTOR_STREAMING=$spoke _tutor_deliver
      [ -n "$_TUTOR_DELIVERED" ] && spoke=1
      if [ -e "$_TUTOR_SESS/pending" ]; then
        :
      elif [ -n "$_TUTOR_EXPECT" ] && [ "$i" -lt 20 ]; then
        :
      else
        break
      fi
      [ "$i" -ge 120 ] && break          # 12s hard cap
      if [ "$i" -eq 15 ] && [ -z "$spoke" ] && [ -z "$hinted" ]; then
        printf '\033[1;35m🧙 …\033[0m ' >&2
        hinted=1
      fi
      sleep 0.1
      i=$((i + 1))
    done
    [ -n "$hinted" ] && [ -z "$spoke" ] && printf '\r\033[K' >&2
    _TUTOR_EXPECT=""
    _TUTOR_STREAMING=$spoke _tutor_deliver
    _TUTOR_STREAMING=""
    return 0
  }

  _tutor_daemon_ok() {
    [ -f "$_TUTOR_SESS/daemon.pid" ] \
      && kill -0 "$(cat "$_TUTOR_SESS/daemon.pid" 2>/dev/null)" 2>/dev/null
  }

  _tutor_spawn_daemon() {
    local root="${GSH_TUTOR_ROOT:-${REAL_HOME:-$HOME}/Documents/gameshell-tutor}"
    [ -f "$root/tutor/tutor_daemon.py" ] || return 1
    command -v python3 >/dev/null 2>&1 || return 1
    # subshell + & = detached from job control, survives without disown
    ( python3 "$root/tutor/tutor_daemon.py" "$_TUTOR_SESS" \
        >> "$_TUTOR_SESS/daemon.log" 2>&1 < /dev/null & ) 2>/dev/null
    return 0
  }

  # the Game Master's own command list — `gsh HELP` is renamed to this in
  # mission briefings (llm.GM_COMMAND_MAP), and FINAL_MISSION points at it
  # for "the list of all GameShell commands"
  _tutor_gm_help() {
    if [ "${LANG%%_*}" = fr ]; then
      cat <<'HELP'
   gm                  te parler librement (ou : gm <question>)
   gm indice           un indice, gradué — jamais la réponse d'abord
   gm mission          te faire raconter l'épreuve en cours
   gm fini             déclencher la validation toi-même
   gm index            la liste des épreuves
   gm goto N           aller à l'épreuve N
   gm parchemin        le parchemin d'origine, inchangé
   gm reset            réinitialiser l'épreuve en cours
   gm persona <nom>    changer ma façon de t'enseigner
   gm quitter          sauvegarder et quitter
HELP
    else
      cat <<'HELP'
   gm                  talk to me freely (or: gm <question>)
   gm hint             a hint, graded, never the answer first
   gm goal             hear the current trial again
   gm check            trigger the check yourself
   gm index            the list of trials
   gm goto N           go to trial N
   gm raw              the original parchment, untouched
   gm reset            reset the current trial
   gm persona <name>   change how I teach you
   gm exit             save and quit
HELP
    fi
  }

  # talk to the Game Master:  gm <free text> | gm [indice] |
  # gm commandes | gm persona <name>
  gm() {
    if ! _tutor_daemon_ok; then
      _tutor_spawn_daemon
      sleep 1
      if ! _tutor_daemon_ok; then
        if [ "${LANG%%_*}" = fr ]; then
          echo "Le Maître du Jeu est absent (démon tuteur arrêté — voir daemon.log)." >&2
        else
          echo "The Game Master is away (tutor daemon not running — see daemon.log)." >&2
        fi
        return 1
      fi
    fi
    # bare `gm`: open a dialogue prompt — free French text is read by
    # `read`, not parsed by bash, so apostrophes (qu'il, c'est…) are safe;
    # typed directly, `gm qu'il…` would leave bash waiting on an open quote
    if [ $# -eq 0 ]; then
      local q
      if [ "${LANG%%_*}" = fr ]; then
        printf '\033[1;35m🧙 Je t'"'"'écoute :\033[0m ' >&2
      else
        printf '\033[1;35m🧙 I am listening:\033[0m ' >&2
      fi
      IFS= read -r -e q < /dev/tty || q=""
      [ -n "$q" ] || return 0
      set -- "$q"
    fi
    local msg="$*"
    case ${1:-} in
      indice|hint) msg="/hint" ;;
      commandes|commands|aide) _tutor_gm_help; return 0 ;;
      persona) shift; msg="/persona $*" ;;
      mission|epreuve|but|goal) gsh goal; return $? ;;
      parchemin|brut|raw) command _gsh_goal; return $? ;;
      fini|valide|check) _tutor_run_check; return $? ;;
      reset) gsh reset; return $? ;;
      # quoted by FINAL_MISSION's text; the GM renames them in the briefing
      # (llm.GM_COMMAND_MAP), so the names it prints must exist here too
      index|sommaire) shift; gsh index "$@"; return $? ;;
      goto|aller) shift; gsh goto "$@"; return $? ;;
      quitter|quitte|exit|pars) gsh exit; return $? ;;
    esac
    _TUTOR_GMID=$((_TUTOR_GMID + 1))
    printf '{"event":"chat","id":%s,"ts":%s,"msg":"%s"}\n' \
      "$_TUTOR_GMID" "$(date +%s)" "$(_tutor_esc "$msg")" >> "$_TUTOR_CHAT"
    # wait, delivering streamed sentences AS THEY ARRIVE; the end of the
    # reply is a *-reply-<id>.msg file — possibly EMPTY (completion marker
    # when everything was already streamed), hence -e and not -s
    local i=0 found="" spoke="" dots=""
    while [ "$i" -lt 300 ]; do            # up to 60s (cold model load)
      set -- "$_TUTOR_OUT"/*-reply-"$_TUTOR_GMID".msg
      local had=""
      [ -e "$1" ] && had=1
      _TUTOR_STREAMING=$spoke _tutor_deliver
      [ -n "$_TUTOR_DELIVERED" ] && spoke=1
      if [ -n "$had" ]; then
        command -p rm -f "$_TUTOR_OUT"/*-reply-"$_TUTOR_GMID".msg 2>/dev/null
        found=1
        break
      fi
      sleep 0.2
      i=$((i + 1))
      if [ -z "$spoke" ]; then
        printf '.' >&2
        dots=1
      fi
    done
    [ -n "$dots" ] && [ -z "$spoke" ] && printf '\r\033[K' >&2
    _TUTOR_STREAMING=$spoke _tutor_deliver
    _TUTOR_STREAMING=""
    if [ -z "$found" ] && [ -z "$spoke" ]; then
      if [ "${LANG%%_*}" = fr ]; then
        echo "Le Maître du Jeu médite… sa réponse viendra à un prochain prompt."
      else
        echo "The Game Master ponders… the answer will come at a later prompt."
      fi
    fi
    return 0
  }
  alias maitre=gm

  # `gsh goal` is narrated by the Game Master. The engine's _gsh_goal is a
  # SCRIPT in the game's PATH; this function shadows it (the gsh dispatcher
  # resolves functions first), and `command _gsh_goal` still reaches the
  # original parchment — used for `gsh goal brut`, explicit mission-number
  # arguments, and every failure path. No engine file is modified.
  _gsh_goal() {
    case ${1:-} in
      brut|raw|parchemin) shift; command _gsh_goal "$@"; return $? ;;
    esac
    if [ -n "$*" ] || ! _tutor_daemon_ok; then
      command _gsh_goal "$@"
      return $?
    fi
    _TUTOR_GMID=$((_TUTOR_GMID + 1))
    printf '{"event":"goal","id":%s,"ts":%s,"mission":"%s"}\n' \
      "$_TUTOR_GMID" "$(date +%s)" "$(_tutor_esc "$MISSION_NB")" >> "$_TUTOR_CHAT"
    local i=0 found=""
    while [ "$i" -lt 125 ]; do
      set -- "$_TUTOR_OUT"/*-reply-"$_TUTOR_GMID".msg
      if [ -s "$1" ]; then found=1; break; fi
      sleep 0.2
      i=$((i + 1))
      printf '.' >&2
    done
    [ "$i" -gt 0 ] && printf '\r\033[K' >&2
    if [ -n "$found" ]; then
      _tutor_deliver
    else
      command _gsh_goal
    fi
    return 0
  }

  _tutor_arm() {
    # MUST stay the last element of PROMPT_COMMAND: the next DEBUG trap
    # after this is the learner's next command line.
    _TUTOR_AT_PROMPT=1
    return 0
  }

  # push delivery: the daemon sends SIGUSR1 when a message is queued and no
  # command is in flight. Display it only if the shell is really idle at the
  # prompt (never inside a command line, a mission's `read`, nano…), then
  # ask readline to redraw the prompt and whatever was half-typed (WINCH).
  _tutor_on_usr1() {
    [ "$_TUTOR_AT_PROMPT" = 1 ] || return 0
    [ -n "$_TUTOR_IN_FLUSH" ] && return 0
    _TUTOR_IN_FLUSH=1
    printf '\r\033[K'
    _tutor_deliver
    _TUTOR_IN_FLUSH=""
    kill -WINCH $$ 2>/dev/null
    return 0
  }

  # session pointer so the tutor pane can find everything
  {
    printf '{"gsh_root":"%s","gsh_config":"%s","gsh_home":"%s","gsh_missions":"%s","pid":%s,"lang":"%s","uid":"%s","shell":"bash"}\n' \
      "$(_tutor_esc "$GSH_ROOT")" "$(_tutor_esc "$GSH_CONFIG")" \
      "$(_tutor_esc "$GSH_HOME")" "$(_tutor_esc "$GSH_MISSIONS")" "$$" \
      "$(_tutor_esc "${LANG%%.*}")" "$(cat "$GSH_CONFIG/uid" 2>/dev/null)" \
      > "$_TUTOR_SESS/session.json"
    printf '%s\n' "$_TUTOR_SESS" > "$_TUTOR_HOME/current-session"
    printf '{"event":"session_start","ts":%s,"mission":"%s"}\n' \
      "$(date +%s)" "$(gsh pcm 2>/dev/null)" >> "$_TUTOR_LOG"
  } 2>/dev/null

  # in-game Game Master: start the tutor daemon (unless the side-pane
  # frontend owns the engine for this session)
  if [ "$GSH_TUTOR_FRONTEND" != "pane" ] && ! _tutor_daemon_ok; then
    _tutor_spawn_daemon
  fi

  # the Game Master IS the interface: mute the engine's "use 'gsh help'"
  # hint and its intro/welcome parchments (the GM's briefing replaces them)
  export GSH_HELP_HINT=never
  export GSH_QUIET_INTRO=1
  # ...and its welcome parchment (gsh-command onboarding): the GM's own
  # mission briefing replaces it. Same shadowing trick as _gsh_goal;
  # `command _gsh_welcome` would still reach the original script.
  _gsh_welcome() { :; }

  # lib/bashrc has already set PROMPT_COMMAND[0]=_check_pwd [1]=_help_hint
  PROMPT_COMMAND=(_tutor_postexec _tutor_deliver_hook "${PROMPT_COMMAND[@]}" _tutor_arm)
  trap '_tutor_preexec' DEBUG
  trap '_tutor_on_usr1' USR1
fi

fi
