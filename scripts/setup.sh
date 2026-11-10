#!/bin/zsh
# One-time setup for ThinkingPartner (macOS 14+, Apple Silicon recommended).
# Installs only what's missing. Choosing an AI model happens later, in the setup screen.
set -e
cd "$(dirname "$0")/.."

command -v brew >/dev/null || { echo "Homebrew is required. Install it from https://brew.sh, then run this again."; exit 1; }
command -v swift >/dev/null || { echo "Xcode Command Line Tools are required. Run: xcode-select --install"; exit 1; }

echo "==> Checking tools…"
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_UPGRADE=1 HOMEBREW_NO_ENV_HINTS=1
missing=()
for tool in cmake ffmpeg; do
  command -v "$tool" >/dev/null || missing+=("$tool")
done
if (( ${#missing} )); then
  echo "==> Installing ${missing[*]} with Homebrew…"
  brew install "${missing[@]}"
fi

echo "==> Building speech-to-text (whisper.cpp)… this takes a few minutes the first time"
[ -d whisper.cpp ] || git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git
(cd whisper.cpp && cmake -B build -DCMAKE_BUILD_TYPE=Release >/dev/null && cmake --build build -j --config Release >/dev/null)
[ -f whisper.cpp/models/ggml-small.en.bin ] || (cd whisper.cpp && bash ./models/download-ggml-model.sh small.en)

echo "==> Building the audio bridge…"
swift build -c release

echo "==> Setting up Python…"
# The Claude library needs Python 3.10+. macOS ships 3.9, so find or install a newer one.
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$(command -v "$cand")"; break
  fi
done
if [ -z "$PY" ]; then
  echo "==> Installing Python 3.12 with Homebrew…"
  brew install python@3.12
  PY="$(brew --prefix python@3.12)/bin/python3.12"
fi
[ -x .venv/bin/python ] && .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null || { rm -rf .venv; "$PY" -m venv .venv; }
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

mkdir -p context
echo
echo "SETUP COMPLETE — start ThinkingPartner with: ./run.command"
echo "You'll choose your AI (free Qwen, Claude or ChatGPT) in the setup screen that opens."
