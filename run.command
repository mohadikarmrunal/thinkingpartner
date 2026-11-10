#!/bin/bash
# ThinkingPartner launcher
set -e
cd "$(dirname "$0")"

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

# Start Ollama in the background if it's installed (the setup screen handles models).
if command -v ollama >/dev/null 2>&1 && ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  ollama serve >/tmp/thinkingpartner-ollama.log 2>&1 &
  sleep 2
fi

python3 server.py &
SERVER_PID=$!
wait "$SERVER_PID"
