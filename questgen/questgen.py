#!/usr/bin/env python3
# questgen.py — generative quests in GameShell's native mission format. GPLv3.
#
# Safety model: the LLM (or the built-in example) only produces a DECLARATIVE
# JSON spec (story, files, predicate). This renderer VALIDATES the spec and
# emits the shell scripts itself, from fixed templates:
#   - the sandbox is a single new directory under $GSH_HOME — nothing outside
#     it is ever touched, and the fork's own mission files are never modified;
#   - the success predicate comes from a whitelist of check types;
#   - LLM output is never executed as shell code.
# The rendered mission uses the same layout as the fork's missions
# (goal/{lang}.txt, static.sh, init.sh, check.sh, clean.sh) and is installed
# with install.sh --quest, which appends it to the mission index.
#
# Usage:
#   ./questgen.py --example --out quest_pigeon_loft     # offline (mock)
#   ./questgen.py --llm --theme "..." --out DIR         # GSH_TUTOR_LLM_URL
#   ./questgen.py --render spec.json --out DIR

import argparse
import json
import os
import re
import sys

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
CHECK_TYPES = {"file_exists", "file_absent", "file_contains", "file_count_equals"}

EXAMPLE_SPEC = {
    "dir": "Pigeonnier",
    "quest_name": "quest_pigeon_loft",
    "goal": {
        "fr": "Objectif\n========\n\nLe maître des pigeons a reçu douze messages cette nuit, mais un seul \nest urgent. Trouvez lequel des fichiers message_* contient le mot \nURGENT (sans ouvrir les douze un par un : pensez à ``grep``), puis \nécrivez le nom de ce fichier dans un fichier ``reponse.txt`` dans le \nPigeonnier.\n\nCommandes utiles\n================\n\ngrep CHAINE FICHIER1 ... FICHIERn\n  Affiche les lignes contenant la chaine (le nom du fichier est \n  indiqué s'il y a plusieurs fichiers).\n",
        "en": "Mission goal\n============\n\nThe pigeon master received twelve messages tonight, but only one is \nurgent. Find which message_* file contains the word URGENT (without \nopening all twelve: think of ``grep``), then write that file's name \ninto a file ``reponse.txt`` in the loft.\n\nUseful commands\n===============\n\ngrep STRING FILE1 ... FILEn\n  Prints the lines containing the string.\n"
    },
    "files": [
        {"path": "message_01", "content": "Le marché aura lieu jeudi.\n"},
        {"path": "message_02", "content": "La récolte de pommes est bonne.\n"},
        {"path": "message_03", "content": "Rien à signaler au moulin.\n"},
        {"path": "message_04", "content": "Le pont est réparé.\n"},
        {"path": "message_05", "content": "URGENT : le dragon approche du village par la Foret !\n"},
        {"path": "message_06", "content": "Les moutons sont rentrés.\n"},
        {"path": "message_07", "content": "Le puits est de nouveau propre.\n"},
        {"path": "message_08", "content": "La foire est reportée.\n"},
        {"path": "message_09", "content": "Le forgeron cherche un apprenti.\n"},
        {"path": "message_10", "content": "Trois poules se sont échappées.\n"},
        {"path": "message_11", "content": "Le boulanger offre du pain.\n"},
        {"path": "message_12", "content": "La chandelle du phare est changée.\n"}
    ],
    "check": {"type": "file_contains", "path": "reponse.txt",
              "needle": "message_05",
              "fail_msg": {"fr": "reponse.txt n'existe pas encore, ou ne contient pas le nom du bon message...",
                           "en": "reponse.txt does not exist yet, or does not contain the right message's name..."}}
}

LLM_SCHEMA_PROMPT = """Invent a small GameShell quest. Reply with ONLY a JSON
object with this exact shape (no prose): {"dir": "OneWordDirName",
"quest_name": "quest_snake_case", "goal": {"fr": "...", "en": "..."},
"files": [{"path": "relative_name", "content": "..."}],
"check": {"type": "file_exists|file_absent|file_contains|file_count_equals",
"path": "relative_name", "needle": "...", "count": 0,
"fail_msg": {"fr": "...", "en": "..."}}}.
Constraints: every path is a plain relative filename inside the quest dir
(no '/', no '..'); the goal must be solvable with standard shell commands;
the check must mechanically verify the goal. Theme: %s"""


def validate(spec):
    def die(msg):
        sys.exit("invalid spec: " + msg)
    if not SAFE_NAME.match(spec.get("dir", "")) or ".." in spec["dir"]:
        die("bad sandbox dir name")
    if not SAFE_NAME.match(spec.get("quest_name", "")):
        die("bad quest_name")
    for f in spec.get("files", []):
        p = f.get("path", "")
        if not SAFE_NAME.match(p) or "/" in p or p.startswith("."):
            die("bad file path: %r" % p)
        if not isinstance(f.get("content"), str) or len(f["content"]) > 20000:
            die("bad file content for %r" % p)
    chk = spec.get("check", {})
    if chk.get("type") not in CHECK_TYPES:
        die("check type must be one of %s" % CHECK_TYPES)
    if not SAFE_NAME.match(chk.get("path", "")):
        die("bad check path")
    if not spec.get("goal", {}).get("en"):
        die("missing goal.en")
    return spec


def sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def render(spec, out_dir):
    validate(spec)
    sandbox = '"$GSH_HOME"/' + sh_quote(spec["dir"])
    os.makedirs(os.path.join(out_dir, "goal"), exist_ok=True)
    for lang, text in spec["goal"].items():
        with open(os.path.join(out_dir, "goal", lang + ".txt"), "w") as f:
            f.write(text)

    with open(os.path.join(out_dir, "static.sh"), "w") as f:
        f.write('#!/usr/bin/env sh\n\nmkdir -p %s\n' % sandbox)

    with open(os.path.join(out_dir, "init.sh"), "w") as f:
        f.write('#!/usr/bin/env sh\n\n_mission_init() (\n  cd %s || return 1\n'
                % sandbox)
        for spec_file in spec["files"]:
            f.write('  cat > %s <<\'GSH_TUTOR_EOF\'\n%sGSH_TUTOR_EOF\n'
                    % (sh_quote(spec_file["path"]), spec_file["content"]))
        f.write(')\n\n_mission_init\n')

    chk = spec["check"]
    path = sandbox + "/" + sh_quote(chk["path"])
    fail_fr = chk.get("fail_msg", {}).get("fr", "")
    fail_en = chk.get("fail_msg", {}).get("en", "not solved yet")
    fail = fail_fr if fail_fr else fail_en
    if chk["type"] == "file_exists":
        cond = '[ -f %s ]' % path
    elif chk["type"] == "file_absent":
        cond = '! [ -e %s ]' % path
    elif chk["type"] == "file_contains":
        cond = 'grep -q %s %s 2>/dev/null' % (sh_quote(chk["needle"]), path)
    else:  # file_count_equals: path is a glob prefix
        cond = ('[ "$(find %s -maxdepth 1 -name %s | wc -l)" -eq %d ]'
                % (sandbox, sh_quote(chk["path"]), int(chk.get("count", 0))))
    with open(os.path.join(out_dir, "check.sh"), "w") as f:
        f.write('#!/usr/bin/env sh\n\n_mission_check() (\n'
                '  if %s\n  then\n    return 0\n  fi\n'
                '  echo %s\n  return 1\n)\n\n_mission_check\n'
                % (cond, sh_quote(fail)))

    with open(os.path.join(out_dir, "clean.sh"), "w") as f:
        f.write('#!/usr/bin/env sh\n\nrm -rf %s\n' % sandbox)

    with open(os.path.join(out_dir, "quest_spec.json"), "w") as f:
        json.dump(spec, f, ensure_ascii=False, indent=1)
    print("quest rendered in %s (install with: install.sh <target> --quest %s)"
          % (out_dir, out_dir))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--example", action="store_true")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--theme", default="a small castle-themed filesystem quest")
    ap.add_argument("--render", metavar="SPEC_JSON")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.render:
        with open(args.render) as f:
            spec = json.load(f)
    elif args.llm:
        sys.path.insert(0, os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "..", "tutor"))
        from llm import make_client, resolve_backend
        client = make_client(resolve_backend())
        if not getattr(client, "url", ""):
            sys.exit("no LLM backend: set GSH_TUTOR_LLM_BACKEND=ollama (or "
                     "GSH_TUTOR_LLM_URL for a generic endpoint), or use "
                     "--example")
        raw = client.respond({"kind": "chat", "lang": "en",
                              "message": LLM_SCHEMA_PROMPT % args.theme})
        try:
            spec = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        except Exception:
            sys.exit("LLM did not return a parseable spec; got:\n" + str(raw))
    else:
        spec = EXAMPLE_SPEC

    render(spec, args.out)


if __name__ == "__main__":
    main()
