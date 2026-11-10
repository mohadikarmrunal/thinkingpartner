# ThinkingPartner

**A live AI thinking partner for your conversations — on a second device, so nothing shows up when you share yours.**

ThinkingPartner listens to a conversation on your Mac, transcribes it on-device, and shows short, glanceable suggestions grounded in *your own documents*. You read them on an iPad, phone, or second laptop. Your Mac's screen stays clean, so you can share it freely.

Use it for interviews, meetings, lectures, and brainstorms.

---

## Why it's different

| | |
|---|---|
| 🖥️ **Nothing on your shared screen** | Suggestions are shown on a second device, not in a window on your Mac. When you share your Mac screen on a call, there's nothing for ThinkingPartner to reveal. |
| 📱 **Any device is the display** | Open a link on your iPad, phone, or another laptop on the same Wi‑Fi. Add it to your home screen for a full-screen view. A pairing token keeps other people on the network out. |
| 📄 **Knows your context** | Drop in your CV, project briefs, meeting notes, or articles. Suggestions draw on them. |
| 🎤 **Both sides of the conversation** | Captures system audio (the other people) and your mic separately, so it knows who said what. |
| 🔒 **Free and private by default** | Transcription (Whisper) and reasoning (Qwen via Ollama) can both run on your Mac. Nothing leaves your computer. |
| ⚡ **Upgrade to Claude or ChatGPT when you want** | Add your own Anthropic or OpenAI API key for stronger answers and screen reading. You pay the provider directly. A spend limit protects you. |

## Modes

| Mode | What it does |
|---|---|
| **Interview** | Helps you answer the current question using your background, keeps case/quant logic consistent |
| **Meeting** | Answers questions aimed at you from your notes, catches decisions and commitments |
| **Learning** | Explains what's being discussed in plain language and suggests a good question to ask |
| **Brainstorm** | Builds on ideas with new angles, contrarian takes, and next steps |

Suggestions follow a fixed glanceable format:

```
STATUS: ANSWER
SAY: One sentence you could say right now.
• Up to three short supporting points
BOTTOM LINE: The one-line takeaway.
```

## Models

| | Model | Cost | Where it runs |
|---|---|---|---|
| **Local** (default) | Qwen 3 via Ollama (`qwen3:8b`; `qwen3:14b` if your Mac can handle it) | Free | Your Mac |
| **Claude** | `claude-opus-5-5` (configurable) | Your Anthropic API key | Anthropic |
| **ChatGPT** | `gpt-5.6-terra` (configurable) | Your OpenAI API key | OpenAI |

Transcription always runs locally (whisper.cpp). Cloud providers only receive the transcript, your documents, and screenshots you choose to send with **Read Screen**.

---

## Requirements

- macOS 14+ (macOS 15+ to capture your microphone). Apple Silicon recommended.
- [Homebrew](https://brew.sh) and Xcode Command Line Tools (setup installs a newer Python if yours is older than 3.10)
- Optional: [Ollama](https://ollama.com) for the free local model

## Getting started

**1. Install** (once). In Terminal:

```bash
git clone https://github.com/mohadikarmrunal/thinkingpartner.git
cd thinkingpartner
./scripts/setup.sh
```

**2. Start it.** Double-click `run.command` (or run `./run.command`). The first time, macOS asks for **Screen Recording** and **Microphone** permission. Allow both, then start it again.

**3. Follow the setup screen** that opens in your browser:

- **Choose which AI to use.** Tick any combination: Qwen (free, on your Mac), Claude, ChatGPT. Setup then walks you through each one:
  - *Qwen, free on this Mac:* checks whether Ollama and Qwen are installed, recommends a model size based on your Mac's memory, and downloads it with one click.
  - *Claude / ChatGPT:* paste your API key (it's verified, then saved in your Keychain). Set one spending limit shared by both.
  - If you set up more than one, pick which to start with. You can switch any time from the buttons at the top of the iPad screen.
- **Add documents** (optional): your CV, briefs, notes. PDF, Word, PowerPoint, Excel, text files and photos or scans all work (scans are read on your Mac). Short, text-based documents work best.
- **Pair your iPad or phone:** scan the QR code with the camera, then Share → Add to Home Screen.

**Every call after that:** double-click `run.command`, open ThinkingPartner on your iPad, pick a mode, and start your call. The pairing link stays the same, so your home-screen icon keeps working.

You can change the model, keys or spending limit any time from **Docs & Settings → Open setup** on the Mac.

## Changing or resetting settings

**Change your AI, keys, spending limit or documents:** on the Mac, open ThinkingPartner, click **Docs & Settings → Open setup**.

**Start setup again from scratch:** stop ThinkingPartner (Ctrl+C in its Terminal window), then run:

```bash
./scripts/reset.sh
```

This resets your choices and the spend counter. Your API keys, documents and downloaded models are kept. It also creates a new iPad/phone link, so delete the old home-screen icon and pair again.

| To also… | Run |
|---|---|
| Remove saved API keys | `./scripts/reset.sh --keys` |
| Remove API keys **and** your documents | `./scripts/reset.sh --all` |
| Only reset the spend counter | Use **reset counter** in setup, or `rm .api-spend.json` |
| Delete a downloaded Qwen model (frees disk space) | `ollama rm qwen3:8b` (use the model name you downloaded) |

## Configuration

Set these environment variables before `./run.command`:

| Variable | Default | Purpose |
|---|---|---|
| `TP_OLLAMA_MODEL` | set in setup | Overrides the local model chosen in setup |
| `TP_CLAUDE_MODEL` | `claude-opus-5-5` | Claude model |
| `TP_CLAUDE_EFFORT` | `low` | Claude reasoning effort (`low` keeps it fast) |
| `TP_OPENAI_MODEL` | `gpt-5.6-terra` | OpenAI model |
| `TP_SPEND_LIMIT_USD` | set in setup | Overrides the spending limit chosen in setup |
| `TP_MODE` | `interview` | Starting mode |
| `TP_PORT` | `8787` | Dashboard port |
| `TP_TOKEN` | saved | Overrides the saved pairing code |
| `TP_OPEN_BROWSER` | `1` | Set to `0` to stop the browser opening on start |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | — | Alternative to saving keys in Keychain |

Your choices are saved in `settings.json` and spending in `.api-spend.json` (both git-ignored). See [Changing or resetting settings](#changing-or-resetting-settings).

## How it works

```
 ┌──────────── Mac ────────────┐            ┌── iPad / phone ──┐
 │ audio-bridge (Swift,        │            │                  │
 │  ScreenCaptureKit)          │            │  Dashboard       │
 │   ├─ system audio → THEM    │            │  (web page)      │
 │   └─ microphone   → YOU     │            │                  │
 │ server.py                   │  Wi‑Fi +   │  suggestions,    │
 │   ├─ whisper.cpp (local)    │──token────▶│  transcripts,    │
 │   ├─ your documents         │            │  pin, modes      │
 │   └─ Qwen / Claude / OpenAI │            │                  │
 └─────────────────────────────┘            └──────────────────┘
```

## Privacy

- Audio is kept as a rolling buffer in `/tmp/thinkingpartner` and is wiped on each start. Nothing is recorded to disk permanently.
- Your documents live in `./context` and are git-ignored.
- In Local mode, nothing leaves your Mac.
- In Claude/ChatGPT mode, the transcript and your documents are sent to that provider under your own API account.

## Use it responsibly

ThinkingPartner captures what other people say. Before you use it:

- **Get consent where the law requires it.** Many places require everyone on a call to agree to it being recorded or transcribed.
- **Follow the rules of the room.** Some interviews, exams, and meetings forbid AI assistance. Using it there anyway is on you.

## License

Copyright 2026 Mrunal Mohadikar. Licensed under the [Apache License, Version 2.0](LICENSE). See `NOTICE`.
