#!/usr/bin/env python3
# test_llm_backends.py — offline verification of the LLM wiring. GPLv3.
#
# Runs a fake OpenAI-compatible server in-process and checks that:
#  - backend "ollama" hits /v1/chat/completions with the OpenAI schema,
#    model "shell-tutor", Bearer not-needed, and NO system message
#    (the Modelfile bakes it);
#  - backend "http" normalizes bare-host / .../v1 / full-endpoint URLs and
#    DOES send the system prompt;
#  - a dead endpoint falls back to the mock (game never breaks);
#  - resolve_backend precedence: env > config > URL-implied > mock.

import hashlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tutor"))
import llm  # noqa: E402

CAPTURED = []


class FakeOpenAI(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        CAPTURED.append({"path": self.path, "body": body,
                         "auth": self.headers.get("Authorization")})
        resp = json.dumps({"choices": [{"message": {
            "role": "assistant", "content": "réponse test"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):
        pass


def check(name, cond):
    print(("  ok  " if cond else "  FAIL") + " " + name)
    if not cond:
        sys.exit(1)


CTX = {"kind": "chat", "lang": "fr", "persona": "socratic_diagnostician",
       "hint_level": 2, "learner": {"mastered": ["ls"]}, "message": "aide"}

server = HTTPServer(("127.0.0.1", 0), FakeOpenAI)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % server.server_port

print("== backend ollama (baked system prompt) ==")
os.environ["GSH_TUTOR_OLLAMA_HOST"] = base
os.environ.pop("GSH_TUTOR_LLM_MODEL", None)
client = llm.make_client("ollama")
reply = client.respond(CTX)
req = CAPTURED[-1]
check("reply parsed", reply == "réponse test")
check("path is /v1/chat/completions", req["path"] == "/v1/chat/completions")
check("model is shell-tutor", req["body"]["model"] == "shell-tutor")
check("auth is Bearer not-needed", req["auth"] == "Bearer not-needed")
roles = [m["role"] for m in req["body"]["messages"]]
check("no system message (baked in Modelfile)", roles == ["user"])
check("context JSON carries dynamic state",
      json.loads(req["body"]["messages"][0]["content"])["hint_level"] == 2)
check("openai fields only",
      set(req["body"]) == {"model", "messages", "max_tokens", "temperature",
                           "stream"})
check("stream off without sink", req["body"]["stream"] is False)

print("== backend ollama honours GSH_TUTOR_LLM_MODEL override ==")
os.environ["GSH_TUTOR_LLM_MODEL"] = "shell-tutor-v2"
llm.make_client("ollama").respond(CTX)
check("model override", CAPTURED[-1]["body"]["model"] == "shell-tutor-v2")
os.environ.pop("GSH_TUTOR_LLM_MODEL")

print("== backend http (generic endpoint, system prompt sent) ==")
for url in (base, base + "/v1", base + "/v1/chat/completions"):
    os.environ["GSH_TUTOR_LLM_URL"] = url
    os.environ["GSH_TUTOR_LLM_MODEL"] = "some-model"
    llm.make_client("http").respond(CTX)
    check("normalized %s" % url,
          CAPTURED[-1]["path"] == "/v1/chat/completions")
roles = [m["role"] for m in CAPTURED[-1]["body"]["messages"]]
check("system prompt present", roles == ["system", "user"])
check("master rule in system prompt",
      "NEVER invent" in CAPTURED[-1]["body"]["messages"][0]["content"])

print("== dead endpoint falls back to mock ==")
os.environ["GSH_TUTOR_OLLAMA_HOST"] = "http://127.0.0.1:9"  # discard port
reply = llm.make_client("ollama").respond(dict(CTX))
check("fallback notice + mock content",
      reply.startswith("[tuteur hors-ligne") and len(reply.splitlines()) > 1)

print("== mission_meta redaction: the ladder is cut at the network edge ==")
META = {"intent": "Be at the top of the tower.",
        "intent_lang": {"fr": "But : le haut du donjon.", "en": "Goal: the top."},
        "hints": {"fr": ["concept fr", "piste fr", "LA COMMANDE fr"],
                  "en": ["concept en", "lead en", "THE COMMAND en"]},
        "idiom_trigger": "cd Chateau",
        "idiom_review": {"fr": "version pro fr", "en": "pro version en"},
        "danger_note": "rm is irreversible",
        "prediction_prompt": {"fr": "predis fr", "en": "predict en"}}


def sent(kind, level, lang="fr"):
    """The context a remote model really receives for this kind/level."""
    os.environ["GSH_TUTOR_OLLAMA_HOST"] = base
    ctx = dict(CTX, kind=kind, lang=lang, hint_level=level, mission_meta=META)
    llm.make_client("ollama").respond(ctx)
    return json.loads(CAPTURED[-1]["body"]["messages"][0]["content"])


for lvl, expected in ((1, []), (2, ["concept fr"]),
                      (3, ["concept fr", "piste fr"]),
                      (4, ["concept fr", "piste fr", "LA COMMANDE fr"])):
    meta = sent("chat", lvl)["mission_meta"]
    check("level %d unlocks %d rung(s)" % (lvl, len(expected)),
          meta.get("hints_unlocked", []) == expected)

body = json.dumps(sent("chat", 1))
check("level 1 leaks no rung at all",
      "LA COMMANDE" not in body and "piste fr" not in body
      and "concept fr" not in body)
check("level 3 still withholds the final command",
      "LA COMMANDE" not in json.dumps(sent("chat", 3)))
check("raw 'hints' key never crosses the wire",
      "hints" not in sent("chat", 4)["mission_meta"])
check("other language is dropped",
      "THE COMMAND en" not in json.dumps(sent("chat", 4, "fr")))
check("prediction_prompt / idiom_trigger dropped",
      "predis fr" not in json.dumps(sent("chat", 4))
      and "cd Chateau" not in json.dumps(sent("chat", 4)))
check("intent kept as grounding",
      sent("chat", 1)["mission_meta"]["intent"] == META["intent"])
check("intent_lang kept, learner's language only",
      sent("chat", 1)["mission_meta"]["intent_lang"] == "But : le haut du donjon.")
check("danger_note only on the danger kind",
      "danger_note" not in sent("chat", 4)["mission_meta"]
      and sent("danger", 1)["mission_meta"]["danger_note"] == "rm is irreversible")
check("idiom_review only after victory",
      "idiom_review" not in sent("chat", 4)["mission_meta"]
      and sent("check_pass", 1)["mission_meta"]["idiom_review"] == "version pro fr")
check("redaction leaves the rest of the context untouched",
      sent("chat", 2)["message"] == "aide")
check("no mission_meta => context passed through",
      llm.redact_context({"kind": "chat"}) == {"kind": "chat"})
original = dict(CTX, mission_meta=META, hint_level=1)
llm.redact_context(original)
check("redact_context does not mutate the caller's ctx",
      original["mission_meta"] is META and "hints" in META)

print("== the mock still sees the full ladder (it gates itself) ==")
mock = llm.MockLLMClient()
check("mock serves rung 3 at level 4",
      mock.respond({"kind": "chat", "lang": "fr", "hint_level": 4,
                    "mission_meta": META, "message": "hint request"})
      == "LA COMMANDE fr")

print("== a lone `gm` never reaches the learner ==")
for junk in ("`gm`", "``gm``", "  gm  ", "gm.", "**gm**", "`gm` :"):
    check("dropped: %r" % junk,
          llm.strip_gm_echo("Va au jardin.\n%s\nQue vois-tu ?" % junk)
          == "Va au jardin.\nQue vois-tu ?")
for keep in ("Tape `gm indice` si tu bloques.",
             "Des questions ? Tape `gm` (puis Entrée) et parle-moi.",
             "Utilise `gm mission` pour réentendre l'épreuve."):
    check("kept: %r" % keep[:34], llm.strip_gm_echo(keep) == keep)
# The model completing the dialogue instead of answering it: it reads the
# learner's own question back as if it were a command to run.
for echo in ("`gm où est-ce que je me trouve ?`", "`gm où suis-je ?`",
             "gm where am I?", "**gm what is a pipe**"):
    check("echoed question dropped: %r" % echo[:30],
          llm.strip_gm_echo("Vous êtes à la maison.\n%s\nOù allez-vous ?" % echo)
          == "Vous êtes à la maison.\nOù allez-vous ?")
for realcmd in ("`gm indice`", "`gm mission`", "gm fini"):
    check("real subcommand kept on its own line: %r" % realcmd,
          llm.strip_gm_echo("Essaie ceci :\n%s" % realcmd)
          == "Essaie ceci :\n%s" % realcmd)
check("trailing lone gm at end of reply dropped",
      llm.strip_gm_echo("Commence par le jardin.\n`gm`") == "Commence par le jardin.")
check("empty/None safe",
      llm.strip_gm_echo("") == "" and llm.strip_gm_echo(None) is None)

print("== the Game Master's word on a victory ==")
VMETA = {"concepts": ["mv", "cd"],
         "idiom_review": {"fr": "version pro fr"}, "idiom_trigger": ""}


def victory(**kw):
    ctx = dict(kind="check_pass", lang="fr", mission_meta=VMETA,
               mission_nb="6", mission_commands=["cd J", "mv a b"],
               learner={"mastered": []}, hint_level=1)
    ctx.update(kw)
    return llm.MockLLMClient().respond(ctx)


check("opens with a congratulation", victory().startswith("🎉"))
check("same mission always opens the same way",
      victory().splitlines()[0] == victory().splitlines()[0])
check("different missions get different openings",
      len({victory(mission_nb=n).splitlines()[0] for n in "123456"}) > 1)
check("a short run is praised",
      "2 commandes" in victory())
check("a long struggle is acknowledged instead",
      "accrocher" in victory(mission_commands=["a"] * 9, hint_level=3))
check("short and struggling are mutually exclusive",
      "accrocher" not in victory())
check("mastered concepts of THIS mission are named",
      "`mv`" in victory(learner={"mastered": ["mv", "cd", "ls"]}))
check("concepts not yet mastered are not claimed",
      "`mv`" not in victory(learner={"mastered": ["ls"]}))
check("idiom review still offered", "version pro fr" in victory())
check("no mission_meta still yields a word",
      victory(mission_meta={}).startswith("🎉"))
check("english too",
      victory(lang="en", mission_meta={}).startswith("🎉")
      and "adventurer" in victory(lang="en", mission_meta={}))

print("== briefings drop commands the GM frontend replaced ==")
GOAL = """Objectif
========

Allez tout en haut.


Commandes utiles
================

cd LIEU
  Se deplace vers le lieu donne.

gsh check
  Verifie si l'objectif de la mission a ete atteint.

gsh reset
  Re-initialise la mission au debut.


Remarque
--------

Lancez ``gsh check`` quand vous saurez la reponse.
"""
brief = llm.MockLLMClient().respond(
    {"kind": "mission_start", "lang": "fr", "mission_goal": GOAL,
     "mission_name": "basic/01", "mission_nb": "1", "mission_meta": {},
     "hint_level": 1}).replace("\x06", "")
check("the `gsh check` command entry is gone",
      "Verifie si l'objectif" not in brief)
check("its description went with it, not just the name",
      "gm fini\n  Verifie" not in brief)
check("`gsh reset` entry survives, renamed",
      "gm reset" in brief and "Re-initialise la mission" in brief)
check("other entries untouched", "cd LIEU" in brief and "Se deplace" in brief)
check("inline prose mentions are kept and renamed",
      "Lancez ``gm fini`` quand vous saurez" in brief)
check("no raw gsh survives the briefing", "gsh " not in brief)

print("== the briefing never hands the learner the solution ==")
LEAKY = {"intent": "Be at the top of the tower.",
         "intent_lang": {"fr": "But : tout en haut (A/B/C/D).",
                         "en": "Goal: the top (A/B/C/D)."}}
brief_fr = llm.MockLLMClient().respond(
    {"kind": "mission_start", "lang": "fr",
     "mission_goal": "Allez tout en haut du donjon.",
     "mission_name": "basic/01", "mission_nb": "1",
     "mission_meta": LEAKY, "hint_level": 1})
check("intent_lang is not shown to the learner", "A/B/C/D" not in brief_fr)
check("the goal text itself is still narrated in full",
      "Allez tout en haut du donjon." in brief_fr)
check("the briefing still opens and closes in character",
      brief_fr.startswith(llm.T["fr"]["brief_intro"])
      and brief_fr.rstrip().endswith(llm.T["fr"]["brief_outro"]))
check("same in english",
      "A/B/C/D" not in llm.MockLLMClient().respond(
          {"kind": "mission_start", "lang": "en", "mission_goal": "Go up.",
           "mission_name": "basic/01", "mission_nb": "1",
           "mission_meta": LEAKY, "hint_level": 1}))
check("but the MODEL still gets intent_lang as grounding",
      sent("chat", 1)["mission_meta"]["intent_lang"] == "But : le haut du donjon.")
POISON = {"intent": "x", "intent_lang": {"fr": "INTENT_LEAK", "en": "INTENT_LEAK"},
          "hints": {"fr": ["RUNG2_LEAK", "RUNG3_LEAK", "RUNG4_LEAK"],
                    "en": ["RUNG2_LEAK", "RUNG3_LEAK", "RUNG4_LEAK"]},
          "idiom_review": {"fr": "IDIOM_LEAK", "en": "IDIOM_LEAK"},
          "danger_note": "DANGER_LEAK"}
for lang in ("fr", "en"):
    b = llm.MockLLMClient().respond(
        {"kind": "mission_start", "lang": lang, "mission_goal": "Montez.",
         "mission_name": "basic/01", "mission_nb": "1",
         "mission_meta": POISON, "hint_level": 4})
    check("[%s] no hint rung reaches a briefing, even at level 4" % lang,
          not any(k in b for k in ("RUNG2_LEAK", "RUNG3_LEAK", "RUNG4_LEAK")))
    check("[%s] no intent_lang, idiom or danger note either" % lang,
          not any(k in b for k in ("INTENT_LEAK", "IDIOM_LEAK", "DANGER_LEAK")))
# The goal text is missing whenever goals-cache is absent or stale, i.e. on
# every fresh machine — so this fallback is not an edge case, and it used to
# splice intent_lang straight into the greeting. For mission 1 that is the
# entire route the learner is supposed to discover with `ls`.
for lang in ("fr", "en"):
    fb = llm.MockLLMClient().respond(
        {"kind": "mission_start", "lang": lang, "mission_goal": "",
         "mission_name": "basic/01", "mission_nb": "1",
         "mission_meta": LEAKY, "hint_level": 1})
    check("[%s] no goal text: the fallback greets without leaking it" % lang,
          bool(fb) and "A/B/C/D" not in fb)

print("== the learner is never told to run a check ==")
WRAPPED = """Objectif
========

Lancez la commande ``gsh check`` pour commencer.

NOTE : reinitialisez avec la commande ``gsh
reset`` si besoin, et votre derniere commande avant ``gsh check`` doit
montrer le total.
"""
b = llm.MockLLMClient().respond(
    {"kind": "mission_start", "lang": "fr", "mission_goal": WRAPPED,
     "mission_name": "x/01", "mission_nb": "1", "mission_meta": {},
     "hint_level": 1})
check("the imperative to run the check is rewritten",
      "Je lance l'epreuve" in b or "Je lance l'épreuve" in b)
check("no `gm fini` anywhere in the briefing", "gm fini" not in b)
check("a command split across lines is still renamed", "gm reset" in b)
check("no raw `gsh` survives", "gsh" not in b)
check("the descriptive constraint keeps its meaning",
      "derniere commande doit" in b)

print("== the mock answers every kind the engine emits ==")
# CLAUDE.md: "The mock must always work ... every kind the engine emits needs
# a mock branch." It is the default backend AND the fallback on any network
# failure, so a kind with no branch is a silent hole in the game. This reads
# the kinds out of engine.py rather than restating them, so a new one cannot
# be added without either a branch here or a failing test.
import re as _re
_eng_src = open(os.path.join(HERE, "..", "tutor", "engine.py")).read()
EMITTED = sorted(set(_re.findall(r'build_context\(\s*"([a-z_]+)"', _eng_src)))
check("engine kinds were found at all", len(EMITTED) >= 6)
_MINIMAL = {"mission_meta": {"hints": {"fr": ["A", "B", "C"],
                                       "en": ["A", "B", "C"]},
                             "intent": "x", "danger_note": "note"},
            "mission_goal": "Objectif\n====\n\nMontez.\n",
            "mission_name": "basic/01", "mission_nb": "1",
            "cmd": "cd nowhere", "exit": 1, "cwd": "/home",
            "output": "bash: cd: nowhere: No such file or directory"}
for _kind in EMITTED:
    for _lang in ("fr", "en"):
        for _level in (1, 4):
            _ctx = dict(_MINIMAL, kind=_kind, lang=_lang, hint_level=_level)
            if _kind == "chat":
                _ctx["message"] = "hint request"
            _out = llm.MockLLMClient().respond(_ctx)
            check("mock answers %-18s [%s, level %d]" % (_kind, _lang, _level),
                  bool(_out and _out.strip()))

print("== the briefing cache key notices the art switch ==")
# mission_art() returns "" when GSH_TUTOR_ART=0 and the renderer honours it,
# but the flag was not in the cache key: one artless session poisoned the
# cache for every art-enabled session afterwards.
import importlib                                             # noqa: E402
_keys = set()
for _art in ("0", "1"):
    os.environ["GSH_TUTOR_ART"] = _art
    _d = importlib.reload(importlib.import_module("tutor_daemon"))
    _keys.add(hashlib.sha1(json.dumps(
        [[llm.T["fr"].get(k, "") for k in
          ("brief_intro", "brief_outro", "greet", "greet_generic")],
         llm.GM_COMMAND_MAP, llm.DROPPED_COMMAND_ENTRIES, _d.MISSION_ART,
         llm.GOAL_SENTENCE_REWRITES, llm.GOAL_SENTENCE_REWRITES_AUTO,
         llm.NARRATION_FORMAT, os.environ.get("GSH_TUTOR_ART") == "0"],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:8])
os.environ.pop("GSH_TUTOR_ART", None)
check("art on and art off get different cache keys", len(_keys) == 2)

print("== resolve_backend precedence ==")
for var in ("GSH_TUTOR_LLM_BACKEND", "GSH_TUTOR_LLM_URL"):
    os.environ.pop(var, None)
check("default mock", llm.resolve_backend({}) == "mock")
check("config wins over default", llm.resolve_backend({"llm": "ollama"}) == "ollama")
os.environ["GSH_TUTOR_LLM_URL"] = base
check("URL implies http", llm.resolve_backend({}) == "http")
os.environ["GSH_TUTOR_LLM_BACKEND"] = "mock"
check("env beats config and URL",
      llm.resolve_backend({"llm": "ollama"}) == "mock")
check("make_client('mock') is MockLLMClient",
      isinstance(llm.make_client("mock"), llm.MockLLMClient))

server.shutdown()
print("ALL BACKEND TESTS PASSED")
