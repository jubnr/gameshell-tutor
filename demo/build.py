#!/usr/bin/env python3
# build.py — generate the browser demo (demo/index.html) from the REAL data:
# the goal texts extracted from the game, the hand-written mission metadata,
# and the mock tutor's own templates. GPLv3.
#
# Usage:  python3 demo/build.py
#
# Nothing here is hand-copied: if a hint ladder or a goal text changes in the
# repo, re-running this regenerates the demo. The output is one self-contained
# HTML file with no external requests, which is what Netlify serves.
#
# The demo covers the first three missions only. They are pure cd/ls/pwd, so a
# small JavaScript filesystem reproduces them exactly, including the engine's
# real check predicates (compare $PWD to a fixed path; mission 3 additionally
# inspects the last two commands). No bash, no WebAssembly, instant load.

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tutor"))
import llm                                      # noqa: E402
from tutor_daemon import MISSION_ART            # noqa: E402

GOALS = os.path.join(os.path.expanduser("~"), ".local/share/gameshell-tutor",
                     "goals-cache")

MISSIONS = [
    {"id": "basic/01_cd_tower", "meta": "basic__01_cd_tower",
     "target": ["Castle", "Main_tower", "First_floor", "Second_floor",
                "Top_of_the_tower"],
     "start": [], "rule": "pwd"},
    {"id": "basic/02_cd.._cellar", "meta": "basic__02_cd.._cellar",
     "target": ["Castle", "Cellar"],
     # the engine leaves you where mission 1 ended: at the top of the tower
     "start": ["Castle", "Main_tower", "First_floor", "Second_floor",
               "Top_of_the_tower"], "rule": "pwd"},
    {"id": "basic/03_cd_HOME_throne", "meta": "basic__03_cd_HOME_throne",
     "target": ["Castle", "Main_building", "Throne_room"],
     # init.sh teleports the player down to the cellar
     "start": ["Castle", "Cellar"], "rule": "pwd+history"},
]

# The world, as recorded by the tutor's own filesystem snapshots. Keys are the
# English names used by the engine's check scripts; the French column is the
# localised directory name the player actually sees with LANG=fr.
WORLD = {
    "Castle": {"fr": "Chateau", "children": {
        "Main_tower": {"fr": "Donjon", "children": {
            "First_floor": {"fr": "Premier_etage", "children": {
                "Second_floor": {"fr": "Deuxieme_etage", "children": {
                    "Top_of_the_tower": {"fr": "Haut_du_donjon",
                                         "children": {}}}}}}}},
        "Main_building": {"fr": "Batiment_principal", "children": {
            "Throne_room": {"fr": "Salle_du_trone", "children": {
                "Kings_quarter": {"fr": "Chambre_du_roi", "children": {}}}},
            "Library": {"fr": "Bibliotheque", "children": {
                "Merlin_s_office": {"fr": "Bureau_de_Merlin",
                                    "children": {}}}}}},
        "Cellar": {"fr": "Cave", "children": {}},
        "Great_hall": {"fr": "Grande_salle", "children": {}},
        "Observatory": {"fr": "Observatoire", "children": {}}}},
    "Forest": {"fr": "Foret", "children": {
        "Hut": {"fr": "Hutte", "children": {
            "Chest": {"fr": "Coffre", "children": {}}}}}},
    "Garden": {"fr": "Jardin", "children": {
        "Maze": {"fr": "Labyrinthe", "children": {}}}},
    "Mountain": {"fr": "Montagne", "children": {
        "Cave": {"fr": "Grotte", "children": {}}}},
    "Stall": {"fr": "Echoppe", "children": {}},
}


def goal_text(meta_name, lang):
    """The mission parchment, narrated the way the Game Master narrates it:
    strip the heading, rename the engine's gsh commands to the gm interface,
    exactly as tutor/llm.py does for a real briefing."""
    path = os.path.join(GOALS, meta_name, lang + ".txt")
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    lines = raw.splitlines()
    while lines and (set(lines[0].strip()) <= {"="} or lines[0].strip().lower()
                     in ("objectif", "mission goal", "obiettivo")):
        lines.pop(0)
    body = "\n".join(lines).strip()
    for old, new in llm.GM_COMMAND_MAP:
        body = body.replace(old, new)
    return body


def collect():
    data = {"missions": [], "art": MISSION_ART["basic"].strip("\n"),
            "world": WORLD, "t": {}}
    for lang in ("fr", "en"):
        keep = ("brief_intro", "brief_outro", "err_no_such_file",
                "err_not_found", "err_generic", "check_fail", "check_pass",
                "idle", "run_and_see", "chat_redirect", "hint_capped")
        data["t"][lang] = {k: llm.T[lang][k] for k in keep}
    for m in MISSIONS:
        with open(os.path.join(ROOT, "tutor", "missions_meta",
                               m["meta"] + ".json"), encoding="utf-8") as f:
            meta = json.load(f)
        data["missions"].append({
            "id": m["id"], "target": m["target"], "start": m["start"],
            "rule": m["rule"],
            "goal": {l: goal_text(m["meta"], l) for l in ("fr", "en")},
            "intent": meta["intent_lang"],
            "hints": meta["hints"],
            "idiom": meta.get("idiom_review", {}),
            "idiom_trigger": meta.get("idiom_trigger", ""),
        })
    return data


def main():
    data = collect()
    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    out = tpl.replace("/*__GAME_DATA__*/null",
                      json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    # optional "full project" link on the end panel; omitted when unset
    out = out.replace("/*__REPO_URL__*/",
                      os.environ.get("DEMO_REPO_URL", ""))
    dest = os.path.join(HERE, "index.html")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(out)
    print("wrote %s (%.1f KB, %d missions)"
          % (dest, len(out) / 1024, len(data["missions"])))


if __name__ == "__main__":
    main()
