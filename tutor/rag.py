# rag.py — local retrieval for the Game Master. GPLv3.
#
# Two commands:
#   python3 rag.py build   — build the index: chunks from (a) the LOCAL man
#       pages of the commands taught by the missions (ground truth for THIS
#       machine's ls/find/grep…), (b) every mission goal text, (c) the
#       missions_meta intents. Embeddings via Ollama (nomic-embed-text),
#       stored as numpy arrays under ~/.local/share/gameshell-tutor/rag/.
#   (library) Retriever.top(query, k) — embed the query, cosine top-k.
#
# The daemon injects the top passages into the LLM context ("knowledge"
# field) for chat and error kinds. This grounds answers in the actual
# system's documentation — the master rule extended to man pages.

import json
import os
import re
import subprocess
import sys
import urllib.request

import numpy as np

TUTOR_HOME = os.environ.get(
    "GSH_TUTOR_HOME",
    os.path.join(os.path.expanduser("~"), ".local/share/gameshell-tutor"))
RAG_DIR = os.path.join(TUTOR_HOME, "rag")
EMBED_MODEL = os.environ.get("GSH_TUTOR_EMBED_MODEL", "nomic-embed-text")
EMBED_TIMEOUT = 10      # live retrieval, on the daemon's critical path
BUILD_TIMEOUT = 120     # index build, offline

COMMANDS = ["ls", "cd", "pwd", "cat", "rm", "mv", "cp", "mkdir", "find",
            "grep", "head", "tail", "less", "nano", "sort", "wc", "cut",
            "tr", "ps", "kill", "pstree", "jobs", "fg", "bg", "alias",
            "chmod", "echo", "man", "xargs", "touch", "cal", "tree"]

CHUNK_LINES = 30


def _embed_url():
    base = os.environ.get("GSH_TUTOR_OLLAMA_HOST", "http://localhost:11434")
    return base.rstrip("/").removesuffix("/v1") + "/api/embed"


def embed(texts, timeout=None):
    # Retrieval happens INSIDE the daemon's single-threaded main loop, so this
    # timeout bounds the whole tutor. It has to stay well under the LLM's own
    # (45s) and under gm()'s wait (60s): at 120s a wedged embedder outlasted
    # both, and the shell held every prompt while nothing could arrive.
    # Index building passes a longer one — nobody is waiting at a prompt then.
    if timeout is None:
        timeout = EMBED_TIMEOUT
    req = urllib.request.Request(
        _embed_url(),
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return np.array(data["embeddings"], dtype=np.float32)


def man_chunks():
    chunks = []
    env = dict(os.environ, MANWIDTH="72")
    for cmd in COMMANDS:
        try:
            out = subprocess.run(
                ["man", cmd], env=env, capture_output=True, text=True,
                timeout=20).stdout
        except Exception:
            continue
        if not out:
            continue
        out = re.sub(r".\x08", "", out)  # strip overstrike bold/underline
        lines = [l.rstrip() for l in out.splitlines()]
        for i in range(0, min(len(lines), 40 * CHUNK_LINES), CHUNK_LINES):
            body = "\n".join(lines[i:i + CHUNK_LINES]).strip()
            if len(body) > 80:
                chunks.append(("man %s" % cmd, body))
    return chunks


def mission_chunks(goals_cache, meta_dir):
    chunks = []
    if os.path.isdir(goals_cache):
        for d in sorted(os.listdir(goals_cache)):
            for langfile in ("fr.txt", "en.txt"):
                p = os.path.join(goals_cache, d, langfile)
                if os.path.isfile(p):
                    with open(p) as f:
                        chunks.append(("mission %s" % d.replace("__", "/"),
                                       f.read().strip()))
    if os.path.isdir(meta_dir):
        for name in sorted(os.listdir(meta_dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(meta_dir, name)) as f:
                    meta = json.load(f)
                chunks.append(("intent %s" % meta.get("mission", name),
                               json.dumps(meta.get("intent_lang", {}),
                                          ensure_ascii=False)))
            except (OSError, json.JSONDecodeError):
                continue
    return chunks


def build():
    here = os.path.dirname(os.path.abspath(__file__))
    chunks = man_chunks()
    chunks += mission_chunks(os.path.join(TUTOR_HOME, "goals-cache"),
                             os.path.join(here, "missions_meta"))
    print("embedding %d chunks…" % len(chunks))
    vecs = []
    for i in range(0, len(chunks), 32):
        vecs.append(embed([c[1] for c in chunks[i:i + 32]],
                          timeout=BUILD_TIMEOUT))
        print("  %d/%d" % (min(i + 32, len(chunks)), len(chunks)))
    mat = np.vstack(vecs)
    mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-8)
    os.makedirs(RAG_DIR, exist_ok=True)
    np.save(os.path.join(RAG_DIR, "vectors.npy"), mat)
    with open(os.path.join(RAG_DIR, "chunks.json"), "w") as f:
        json.dump(chunks, f, ensure_ascii=False)
    print("index: %d chunks -> %s" % (len(chunks), RAG_DIR))


class Retriever:
    def __init__(self):
        self.ok = False
        try:
            self.mat = np.load(os.path.join(RAG_DIR, "vectors.npy"))
            with open(os.path.join(RAG_DIR, "chunks.json")) as f:
                self.chunks = json.load(f)
            self.ok = len(self.chunks) == self.mat.shape[0]
        except Exception:
            pass

    def top(self, query, k=3, max_chars=1400):
        """[(source, text)] most relevant to the query; [] on any failure —
        retrieval must never break the tutor."""
        if not self.ok or not query:
            return []
        try:
            q = embed([query])[0]
            q /= (np.linalg.norm(q) + 1e-8)
            scores = self.mat @ q
            picked, used = [], 0
            for i in np.argsort(-scores)[:k * 2]:
                src, text = self.chunks[int(i)]
                if used + len(text) > max_chars:
                    text = text[:max_chars - used]
                picked.append({"source": src, "text": text})
                used += len(text)
                if used >= max_chars or len(picked) >= k:
                    break
            return picked
        except Exception:
            return []


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        build()
    else:
        r = Retriever()
        print("index ok:", r.ok)
        for p in r.top(" ".join(sys.argv[1:]) or "comment chercher un fichier"):
            print("--", p["source"], "--")
            print(p["text"][:200])
