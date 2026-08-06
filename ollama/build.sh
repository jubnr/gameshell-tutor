#!/usr/bin/env bash
# build.sh — build and smoke-test the frozen shell-tutor model on Ollama. GPLv3.
# Usage: ./build.sh [ollama-host]     (default http://localhost:11434)
set -e
HERE=$(cd "$(dirname "$0")"; pwd -P)
HOST=${1:-${GSH_TUTOR_OLLAMA_HOST:-http://localhost:11434}}
HOST=${HOST%/}
HOST=${HOST%/v1}

if ! curl -fsS --max-time 3 "$HOST/api/version" >/dev/null; then
  echo "error: no Ollama server at $HOST (start it with 'ollama serve'," >&2
  echo "or pass the GPU host: ./build.sh http://<gpu-host-ip>:11434)" >&2
  exit 1
fi

if [ "$HOST" = "http://localhost:11434" ] && command -v ollama >/dev/null; then
  ollama create shell-tutor -f "$HERE/Modelfile"
else
  # remote host: use the HTTP API (files= inline Modelfile is not portable
  # across versions, so require the base model there and create remotely)
  echo "building on remote $HOST via API..."
  python3 - "$HOST" "$HERE/Modelfile" <<'EOF'
import json, sys, urllib.request
host, path = sys.argv[1], sys.argv[2]
req = urllib.request.Request(host + "/api/create",
    data=json.dumps({"model": "shell-tutor",
                     "modelfile": open(path).read()}).encode(),
    headers={"Content-Type": "application/json"})
for line in urllib.request.urlopen(req, timeout=600):
    status = json.loads(line).get("status", "")
    if status: print("  " + status)
EOF
fi

echo "== smoke test: OpenAI-compatible endpoint =="
python3 - "$HOST" <<'EOF'
import json, sys, urllib.request
host = sys.argv[1]
ctx = {"kind": "chat", "lang": "fr", "persona": "socratic_diagnostician",
       "hint_level": 1, "mission_nb": "1", "cmd": None, "output": None,
       "learner": {"mastered": [], "used": [], "recent_errors": []},
       "message": "qu'est-ce que la commande ls va afficher ici ?"}
req = urllib.request.Request(host + "/v1/chat/completions",
    data=json.dumps({"model": "shell-tutor", "max_tokens": 200,
        "messages": [{"role": "user",
                      "content": json.dumps(ctx, ensure_ascii=False)}]}).encode(),
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer not-needed"})
data = json.load(urllib.request.urlopen(req, timeout=120))
reply = data["choices"][0]["message"]["content"]
print("tutor reply:\n  " + reply.replace("\n", "\n  "))
print("\nGROUNDING CHECK: output was null — the reply above must ask the")
print("learner to RUN the command and observe, not describe ls's output.")
EOF
echo "== done. Enable with: export GSH_TUTOR_LLM_BACKEND=ollama =="
