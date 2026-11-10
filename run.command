#!/bin/bash
# ThinkingPartner launcher
set -e
cd "$(dirname "$0")"

AUDIO_DIR="${TP_AUDIO_DIR:-/tmp/thinkingpartner}"

pkill -f "/audio-bridge" 2>/dev/null || true
pkill -f "python[0-9.]* server.py" 2>/dev/null || true
rm -rf "$AUDIO_DIR"
mkdir -p "$AUDIO_DIR"

cleanup() {
  [ -n "$AUDIO_PID" ] && kill "$AUDIO_PID" 2>/dev/null || true
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Rebuild the audio bridge if the code changed (instant when nothing changed).
swift build -c release >/dev/null 2>&1 || echo "Note: could not rebuild the audio bridge; run ./scripts/setup.sh"

echo "Starting audio capture…"
.build/release/audio-bridge &
AUDIO_PID=$!
sleep 2

# Start Ollama in the background if it's installed (the setup screen handles models).
if command -v ollama >/dev/null 2>&1 && ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  ollama serve >/tmp/thinkingpartner-ollama.log 2>&1 &
  sleep 2
fi

PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
"$PY" server.py &
SERVER_PID=$!
wait "$SERVER_PID"
