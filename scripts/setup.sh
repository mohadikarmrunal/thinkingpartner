#!/bin/zsh
# One-time setup for ThinkingPartner (macOS 14+, Apple Silicon recommended)
set -e
cd "$(dirname "$0")/.."
command -v brew >/dev/null || { echo "Homebrew required: https://brew.sh"; exit 1; }
brew install cmake ffmpeg
[ -d whisper.cpp ] || git clone https://github.com/ggml-org/whisper.cpp.git
(cd whisper.cpp && cmake -B build && cmake --build build -j && bash ./models/download-ggml-model.sh small.en)
swift build -c release
python3 -m pip install --user -r requirements.txt
if command -v ollama >/dev/null 2>&1; then
  ollama pull "${TP_OLLAMA_MODEL:-qwen3:8b}"
else
  echo "Optional: install Ollama (https://ollama.com) for the free on-device model."
fi
mkdir -p context
echo "SETUP COMPLETE — start with ./run.command"
