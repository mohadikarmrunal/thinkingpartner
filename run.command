#!/bin/bash
# ThinkingPartner launcher
set -e
cd "$(dirname "$0")"

OLLAMA_MODEL="${TP_OLLAMA_MODEL:-qwen3:8b}"
AUDIO_DIR="${TP_AUDIO_DIR:-/tmp/thinkingpartner}"

pkill -f "/audio-bridge" 2>/dev/null || true
pkill -f "python3 server.py" 2>/dev/null || true
rm -rf "$AUDIO_DIR"
mkdir -p "$AUDIO_DIR"

cleanup() {
  [ -n "$AUDIO_PID" ] && kill "$AUDIO_PID" 2>/dev/null || true
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Starting audio capture…"
.build/release/audio-bridge &
AUDIO_PID=$!
sleep 2

if command -v ollama >/dev/null 2>&1; then
  if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    echo "Starting Ollama…"
    ollama serve >/tmp/thinkingpartner-ollama.log 2>&1 &
    sleep 2
  fi
  if ! ollama list 2>/dev/null | grep -q "^${OLLAMA_MODEL}"; then
    echo "NOTE: local model ${OLLAMA_MODEL} is not installed. Run: ollama pull ${OLLAMA_MODEL}"
    echo "      (Claude / ChatGPT still work if you add an API key.)"
  fi
else
  echo "NOTE: Ollama not found — the free local model is unavailable. Install from https://ollama.com"
fi

python3 server.py &
SERVER_PID=$!
wait "$SERVER_PID"
