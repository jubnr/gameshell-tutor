#!/usr/bin/env sh

_mission_check() (
  if grep -q 'message_05' "$GSH_HOME"/'Pigeonnier'/'reponse.txt' 2>/dev/null
  then
    return 0
  fi
  echo 'reponse.txt n'\''existe pas encore, ou ne contient pas le nom du bon message...'
  return 1
)

_mission_check
