#!/usr/bin/env bash
# enable-igpu.sh — put the tutor model on an integrated GPU, and prove it.
# GPLv3.
#
# Usage:  sudo bash ollama/enable-igpu.sh [vulkan|rocm [GFXVER]|--revert]
#
#   vulkan   (default) Ollama's Vulkan backend. Needs a Vulkan driver (Mesa
#            RADV for AMD, ANV for Intel) and NO gfx override: the driver
#            compiles shaders for the GPU actually present, so it cannot hit
#            the ISA mismatch that breaks ROCm on unsupported iGPUs.
#   rocm     ROCm/HIP with HSA_OVERRIDE_GFX_VERSION (default 11.0.2). Faster
#            than Vulkan WHEN the override matches: ROCm ships kernels only
#            for supported targets, and running gfx1102 code on a gfx1103
#            (Radeon 780M) dies with "ROCm error: unspecified launch failure"
#            — the model loads into VRAM, then the first kernel crashes.
#            Find your target with:
#              grep gfx_target_version /sys/class/kfd/kfd/topology/nodes/*/properties
#            110003 = gfx1103, 110002 = gfx1102, 103500 = gfx1035 (->10.3.0).
#   --revert back to CPU.
#
# SAFETY: if the model fails to answer after the switch, this script reverts
# itself and restarts Ollama, so you are never left with a broken tutor.

set -u
CONF=/etc/systemd/system/ollama.service.d/igpu.conf
MODEL=${GSH_TUTOR_LLM_MODEL:-shell-tutor}
CPU_BASELINE=9.3   # tok/s measured on this machine before any offload

[ "$(id -u)" = 0 ] || { echo "run me with sudo: sudo bash $0 $*" >&2; exit 1; }

restart() {
  systemctl daemon-reload
  systemctl restart ollama
  printf '  waiting for ollama'
  for _ in $(seq 1 60); do
    curl -fsS --max-time 2 http://localhost:11434/api/version >/dev/null 2>&1 && break
    printf '.'; sleep 1
  done
  echo " $(systemctl is-active ollama)"
}

revert() {
  rm -f "$CONF"
  restart
  echo "[ reverted: Ollama is back on CPU ]"
}

MODE=${1:-vulkan}
case $MODE in
  --revert|revert) revert; exit 0 ;;
  vulkan)
    mkdir -p "$(dirname "$CONF")"
    cat > "$CONF" <<'EOF'
# written by ollama/enable-igpu.sh — Vulkan backend on the integrated GPU
[Service]
Environment="OLLAMA_IGPU_ENABLE=1"
Environment="OLLAMA_VULKAN=1"
EOF
    ;;
  rocm)
    GFX=${2:-11.0.2}
    mkdir -p "$(dirname "$CONF")"
    cat > "$CONF" <<EOF
# written by ollama/enable-igpu.sh — ROCm backend on the integrated GPU
[Service]
Environment="OLLAMA_IGPU_ENABLE=1"
Environment="HSA_OVERRIDE_GFX_VERSION=$GFX"
EOF
    ;;
  *) echo "unknown mode: $MODE (want: vulkan | rocm [GFXVER] | --revert)" >&2; exit 1 ;;
esac

echo "[ installed $CONF ]"
sed -n 's/^Environment=/    /p' "$CONF"
restart

echo
echo "== benchmark (model $MODEL, mode $MODE) =="
python3 - "$MODEL" "$CPU_BASELINE" <<'EOF'
import json, sys, time, urllib.request
H, model, baseline = "http://localhost:11434", sys.argv[1], float(sys.argv[2])
def post(path, body, timeout=600):
    req = urllib.request.Request(H + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))
best, reply = 0.0, ""
try:
    for i in range(3):
        d = post("/api/chat", {"model": model, "stream": False,
                               "options": {"num_predict": 80, "temperature": 0.3},
                               "messages": [{"role": "user", "content":
                                             "Explique en trois phrases ce que fait la commande cd."}]})
        best = max(best, d.get("eval_count", 0) /
                   max(d.get("eval_duration", 1) / 1e9, 1e-9))
        reply = d["message"]["content"].replace("\n", " ")[:130]
except Exception as exc:
    body = getattr(exc, "read", lambda: b"")()
    print("  FAILED: %s %s" % (exc, body.decode("utf-8", "replace")[:200]))
    sys.exit(2)          # tells the shell to auto-revert
vram = 0
for m in json.load(urllib.request.urlopen(H + "/api/ps", timeout=10)).get("models", []):
    if m["name"].startswith(model):
        vram = m.get("size_vram", 0)
print("  sample reply : %s" % reply)
print("  generation   : %.1f tok/s   (CPU baseline %.1f)" % (best, baseline))
print("  size_vram    : %.2f GB" % (vram / 1e9))
print()
if vram == 0:
    print("  VERDICT: still on CPU — the GPU was not used at all.")
elif best < baseline * 1.3:
    print("  VERDICT: on the GPU, but not meaningfully faster.")
    print("           An iGPU shares system RAM, so bandwidth caps the gain.")
    print("           Revert if you prefer fewer moving parts:")
    print("             sudo bash ollama/enable-igpu.sh --revert")
else:
    print("  VERDICT: working, %.1fx faster than CPU — keep it." % (best / baseline))
EOF
rc=$?

if [ "$rc" = 2 ]; then
  echo
  echo "[ the model could not answer on this backend — reverting automatically ]"
  revert
  if [ "$MODE" = vulkan ]; then
    echo "  Vulkan did not work either; CPU is the reliable option here."
  else
    echo "  next thing to try:  sudo bash $0 vulkan"
  fi
  exit 1
fi
