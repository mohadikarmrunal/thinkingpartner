#!/usr/bin/env python3
# Copyright 2026 Mrunal Mohadikar
# SPDX-License-Identifier: Apache-2.0
"""ThinkingPartner — a live AI thinking partner for conversations.

Captures system audio (the other side) and your microphone, transcribes locally
with whisper.cpp, and generates glanceable suggestions grounded in your own
documents. Suggestions are served to any device on your network (iPad, phone,
second laptop) so nothing has to appear on the screen you are sharing.

Reasoning providers:
  local   Qwen via Ollama (free, fully on-device)
  claude  Anthropic Claude (bring your own API key)
  openai  OpenAI / ChatGPT (bring your own API key)
"""
import base64
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

APP_NAME = "ThinkingPartner"
ROOT = Path(__file__).resolve().parent
AUDIO_DIR = Path(os.environ.get("TP_AUDIO_DIR", "/tmp/thinkingpartner"))
PUBLIC = ROOT / "public"
CONTEXT_DIR = ROOT / "context"
LEGACY_PROFILE = ROOT / "interview_profile.txt"
WHISPER = ROOT / "whisper.cpp" / "build" / "bin" / "whisper-cli"
WHISPER_MODEL = ROOT / "whisper.cpp" / "models" / os.environ.get("TP_WHISPER_MODEL", "ggml-small.en.bin")
SPEND_FILE = ROOT / ".api-spend.json"
SCREENSHOT_FILE = AUDIO_DIR / "screenshot.jpg"
SCREENSHOT_REQUEST = AUDIO_DIR / "screenshot.request"
PORT = int(os.environ.get("TP_PORT", "8787"))
SETTINGS_FILE = ROOT / "settings.json"
BRIDGE = ROOT / ".build" / "release" / "audio-bridge"

OLLAMA_URL = os.environ.get("TP_OLLAMA_URL", "http://127.0.0.1:11434")

# Local models offered in setup, smallest first. Sizes are approximate download sizes.
LOCAL_MODELS = [
    {"name": "qwen3:4b", "size_gb": 2.6, "min_ram_gb": 8,
     "blurb": "Fastest. Fine for simple meetings; weaker on hard reasoning."},
    {"name": "qwen3:8b", "size_gb": 5.2, "min_ram_gb": 16,
     "blurb": "Best balance of speed and quality for most Macs."},
    {"name": "qwen3:14b", "size_gb": 9.3, "min_ram_gb": 32,
     "blurb": "Smartest local option. Needs a Mac with lots of memory."},
]


def load_settings():
    defaults = {"setup_complete": False, "provider": "local", "local_model": "qwen3:8b",
                "spend_limit_usd": 10.0, "token": secrets.token_urlsafe(6)}
    try:
        defaults.update(json.loads(SETTINGS_FILE.read_text()))
    except Exception:
        pass
    # Environment variables override saved settings (handy for power users and testing).
    if os.environ.get("TP_OLLAMA_MODEL"):
        defaults["local_model"] = os.environ["TP_OLLAMA_MODEL"]
    if os.environ.get("TP_SPEND_LIMIT_USD"):
        defaults["spend_limit_usd"] = float(os.environ["TP_SPEND_LIMIT_USD"])
    if os.environ.get("TP_TOKEN"):
        defaults["token"] = os.environ["TP_TOKEN"]
    return defaults


SETTINGS = load_settings()


def save_settings():
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(SETTINGS, indent=2))
    os.replace(tmp, SETTINGS_FILE)


save_settings()  # persists the pairing token so the iPad link keeps working across restarts


def local_model():
    return SETTINGS["local_model"]


def spend_limit():
    return float(SETTINGS["spend_limit_usd"])

# Cloud models (bring your own key). Prices are USD per 1M tokens and only drive the local spend guard.
CLAUDE_MODEL = os.environ.get("TP_CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.environ.get("TP_CLAUDE_EFFORT", "low")  # low keeps live latency down
OPENAI_MODEL = os.environ.get("TP_OPENAI_MODEL", "gpt-5.6-terra")
PRICES = {
    "claude": {
        "input": float(os.environ.get("TP_CLAUDE_PRICE_IN", "4.00")),
        "cached": float(os.environ.get("TP_CLAUDE_PRICE_CACHED", "0.20")),
        "output": float(os.environ.get("TP_CLAUDE_PRICE_OUT", "20.00")),
    },
    "openai": {
        "input": float(os.environ.get("TP_OPENAI_PRICE_IN", "2.00")),
        "cached": float(os.environ.get("TP_OPENAI_PRICE_CACHED", "0.20")),
        "output": float(os.environ.get("TP_OPENAI_PRICE_OUT", "12.00")),
    },
}
# Models that accept Anthropic's server-side refusal fallback.
CLAUDE_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")

CLOUD_MAX_OUTPUT = 8000

KEYCHAIN = {
    "claude": ["ThinkingPartner-Anthropic"],
    "openai": ["ThinkingPartner-OpenAI", "InterviewCopilot-OpenAI"],
}
ENV_KEYS = {"claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

PROVIDERS = ("local", "claude", "openai")


def provider_label(p):
    return {"local": f"Local · {local_model()}", "claude": f"Claude · {CLAUDE_MODEL}",
            "openai": f"ChatGPT · {OPENAI_MODEL}"}[p]


# 16 kHz * 2 bytes * 4 sec
CHUNK_BYTES = 128000

# Devices on your network must present this token (in the pairing link) to read the feed.
ACCESS_TOKEN = SETTINGS["token"]

CONTEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".json", ".pdf", ".docx", ".rtf", ".html"}
MAX_CONTEXT_CHARS = int(os.environ.get("TP_MAX_CONTEXT_CHARS", "60000"))


# ---------------------------------------------------------------- modes

MODES = {
    "interview": {
        "label": "Interview",
        "them": "INTERVIEWER",
        "guide": (
            "The user is being interviewed. THEM is the interviewer. Help the user answer the "
            "current question well, drawing on their background documents. For case or "
            "quantitative questions, preserve earlier numbers and assumptions."
        ),
    },
    "meeting": {
        "label": "Meeting",
        "them": "OTHERS",
        "guide": (
            "The user is in a work meeting. Help them respond to questions or points directed at "
            "them using facts from their documents, catch commitments and decisions, and flag "
            "anything that contradicts their notes."
        ),
    },
    "learning": {
        "label": "Learning",
        "them": "SPEAKER",
        "guide": (
            "The user is learning — a lecture, tutorial, or expert conversation. Explain the "
            "concept currently being discussed in plain language, connect it to the user's "
            "notes, and suggest one sharp question they could ask."
        ),
    },
    "brainstorm": {
        "label": "Brainstorm",
        "them": "OTHERS",
        "guide": (
            "The user is brainstorming. Build on the ideas being discussed: offer a new angle, "
            "a contrarian take, or a concrete next step, grounded in the user's project context."
        ),
    },
}
DEFAULT_MODE = os.environ.get("TP_MODE", "interview")

OUTPUT_FORMAT = """Keep it glanceable — the user reads this while talking. No essays.
Use exactly this format:
STATUS: <ON TRACK | REDIRECT | CORRECT | ANSWER | IDEA>
SAY: <one sentence the user could say right now>
• <up to 3 short bullets; omit unnecessary bullets>
BOTTOM LINE: <one short conclusion>

Never repeat advice already covered unless correcting it. Never invent facts; mark
assumptions explicitly. If no intervention is useful right now, return exactly: WAIT"""


def system_prompt(mode):
    m = MODES.get(mode, MODES[DEFAULT_MODE])
    return (
        f"You are {APP_NAME}, a silent real-time thinking partner shown on the user's second "
        f"screen. You receive a rolling transcript with {m['them']} (labelled THEM) and the "
        "user (labelled YOU), plus the user's own reference documents. Treat both speakers as "
        "context and judge against the whole exchange, not just the latest line.\n\n"
        f"Situation: {m['guide']}\n\n{OUTPUT_FORMAT}"
    )


LOCAL_CLASSIFIER = """You decide whether a live assistant should speak up.
Read the recent dialogue between THEM and YOU. Return exactly one label:
ANSWER  = THEM asked a question or made a request that YOU has not yet adequately handled.
COACH   = YOU is drifting, contradicting earlier logic, missing something important, or needs a next step.
IMPROVE = YOU is broadly on track but a concise addition or sharper framing would clearly help.
WAIT    = no intervention is useful right now."""


# ---------------------------------------------------------------- state

def load_spend():
    try:
        return max(0.0, float(json.loads(SPEND_FILE.read_text()).get("spent_usd", 0.0)))
    except Exception:
        return 0.0


def save_spend(value):
    tmp = SPEND_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"spent_usd": round(value, 8), "updated": time.time()}))
    os.replace(tmp, SPEND_FILE)


state = {
    "app": APP_NAME,
    "them": "",
    "you": "",
    "suggestion": "",
    "reasoning_status": "Waiting for the conversation…",
    "status": "Starting…",
    "updated": 0,
    "provider": SETTINGS["provider"],
    "provider_label": provider_label(SETTINGS["provider"]),
    "setup_complete": bool(SETTINGS["setup_complete"]),
    "providers": {},
    "mode": DEFAULT_MODE if DEFAULT_MODE in MODES else "interview",
    "modes": {k: v["label"] for k, v in MODES.items()},
    "spend_usd": load_spend(),
    "spend_limit_usd": spend_limit(),
    "screen_status": "No screen context",
    "screen_context": "",
    "context_files": [],
}

history = []
last_reasoned_text = ""
last_reasoned_at = 0.0
reasoning_lock = threading.Lock()
lock = threading.Lock()


# ---------------------------------------------------------------- documents

def extract_text(path):
    ext = path.suffix.lower()
    if ext in (".txt", ".md", ".markdown", ".csv", ".json"):
        return path.read_text(errors="ignore")
    if ext in (".docx", ".rtf", ".html"):
        cp = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)],
                            capture_output=True, text=True, timeout=20)
        return cp.stdout if cp.returncode == 0 else ""
    if ext == ".pdf":
        try:
            from pypdf import PdfReader  # optional: pip install pypdf
            return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
        except ImportError:
            return "[PDF skipped — run `pip3 install pypdf` to read PDFs]"
        except Exception as e:
            return f"[Could not read PDF: {e}]"
    return ""


_context_cache = {"sig": None, "text": "", "files": []}


def load_context():
    """Concatenate the user's documents from ./context (cached until files change)."""
    CONTEXT_DIR.mkdir(exist_ok=True)
    paths = sorted(p for p in CONTEXT_DIR.iterdir()
                   if p.is_file() and p.suffix.lower() in CONTEXT_EXTS and p.name.lower() != "readme.md")
    if not paths and LEGACY_PROFILE.exists():
        paths = [LEGACY_PROFILE]
    sig = tuple((p.name, p.stat().st_mtime, p.stat().st_size) for p in paths)
    if sig == _context_cache["sig"]:
        return _context_cache["text"]
    parts, files, used = [], [], 0
    for p in paths:
        text = extract_text(p).strip()
        if not text:
            continue
        room = MAX_CONTEXT_CHARS - used
        if room <= 0:
            files.append({"name": p.name, "chars": len(text), "included": False})
            continue
        clipped = text[:room]
        used += len(clipped)
        parts.append(f"=== {p.name} ===\n{clipped}")
        files.append({"name": p.name, "chars": len(text), "included": len(clipped) == len(text)})
    _context_cache.update(sig=sig, text="\n\n".join(parts), files=files)
    with lock:
        state["context_files"] = files
    return _context_cache["text"]


def safe_filename(name):
    name = os.path.basename(name or "").strip().replace("\x00", "")
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:120]
    return name if name and not name.startswith(".") else ""


# ---------------------------------------------------------------- transcription

def clean_text(s):
    """Keep transcript text; discard whisper.cpp diagnostics."""
    kept = []
    prefixes = ("whisper_", "ggml_", "system_info", "main:", "read_audio_data:",
                "output_", "sampling_", "compute_", "encode_", "decode_")
    for raw in s.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith(prefixes):
            continue
        m = re.match(r"^\[[0-9:.]+\s*-->\s*[0-9:.]+\]\s*(.*)$", line)
        if m:
            line = m.group(1).strip()
        if any(x in low for x in ("reading audio data from", "processing ", "mel =", "n_threads =",
                                  "n_processors =", "model loaded", "load time", "sample time",
                                  "encode time", "decode time", "batchd time", "prompt time", "total time")):
            continue
        kept.append(line)
    return " ".join(kept).strip()


def transcribe(raw):
    with tempfile.TemporaryDirectory(prefix="tp-whisper-") as td:
        td = Path(td)
        pcm, wav = td / "chunk.pcm", td / "chunk.wav"
        pcm.write_bytes(raw)
        ff = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "s16le", "-ar", "16000",
                             "-ac", "1", "-i", str(pcm), str(wav)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ff.returncode != 0:
            return ""
        outbase = td / "transcript"
        cp = subprocess.run([str(WHISPER), "-m", str(WHISPER_MODEL), "-f", str(wav),
                             "-otxt", "-of", str(outbase), "-nt", "-np"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        txt = Path(str(outbase) + ".txt")
        return clean_text(txt.read_text(errors="ignore")) if cp.returncode == 0 and txt.exists() else ""


# ---------------------------------------------------------------- keys & spend

def get_key(provider):
    env = os.environ.get(ENV_KEYS[provider], "").strip()
    if env:
        return env
    for service in KEYCHAIN[provider]:
        cp = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                            capture_output=True, text=True, timeout=5)
        if cp.returncode == 0 and cp.stdout.strip():
            return cp.stdout.strip()
    return ""


def store_key(provider, key):
    service = KEYCHAIN[provider][0]
    cp = subprocess.run(["security", "add-generic-password", "-U", "-a", os.environ.get("USER", APP_NAME),
                         "-s", service, "-w", key], capture_output=True, text=True, timeout=5)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or "Keychain write failed")


def refresh_provider_availability():
    installed = {m["name"] for m in (ollama_models() or [])}
    avail = {"local": local_model() in installed,
             "claude": bool(get_key("claude")), "openai": bool(get_key("openai"))}
    with lock:
        state["providers"] = avail
    return avail


def reserve_check(provider, chars_in):
    rates = PRICES[provider]
    est = ((chars_in // 3 + 1) * rates["input"] + CLOUD_MAX_OUTPUT * rates["output"]) / 1_000_000
    with lock:
        if state["spend_usd"] + est > spend_limit():
            raise RuntimeError("SPEND_LIMIT")


def record_spend(provider, uncached_in, cached_in, out):
    rates = PRICES[provider]
    cost = (uncached_in * rates["input"] + cached_in * rates["cached"] + out * rates["output"]) / 1_000_000
    with lock:
        state["spend_usd"] = state["spend_usd"] + cost
        save_spend(state["spend_usd"])


# ---------------------------------------------------------------- first-run setup

def system_ram_gb():
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3).stdout
        return round(int(out.strip()) / 1024 ** 3)
    except Exception:
        return 0


RAM_GB = system_ram_gb()


def recommended_local_model():
    # Leave headroom for the call app, browser and Whisper: pick the largest model whose
    # memory requirement fits.
    best = LOCAL_MODELS[0]["name"]
    for m in LOCAL_MODELS:
        if RAM_GB >= m["min_ram_gb"]:
            best = m["name"]
    return best


def ollama_app_installed():
    import shutil
    return bool(shutil.which("ollama")) or Path("/Applications/Ollama.app").exists()


def ollama_models():
    """Installed Ollama models, or None if the Ollama server isn't reachable."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2) as r:
            data = json.loads(r.read().decode())
        return [{"name": m.get("name", ""), "size_gb": round((m.get("size") or 0) / 1e9, 1)}
                for m in data.get("models", [])]
    except Exception:
        return None


def ollama_models_dir():
    return os.environ.get("OLLAMA_MODELS") or str(Path.home() / ".ollama" / "models")


def start_ollama():
    if Path("/Applications/Ollama.app").exists():
        subprocess.Popen(["open", "-a", "Ollama"])
    else:
        import shutil
        exe = shutil.which("ollama")
        if not exe:
            raise RuntimeError("Ollama is not installed")
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    for _ in range(40):
        if ollama_models() is not None:
            return
        time.sleep(0.25)
    raise RuntimeError("Ollama didn't start — open the Ollama app once, then try again")


pull_state = {"model": "", "status": "", "completed": 0, "total": 0, "error": "", "active": False}


def pull_model(name):
    pull_state.update(model=name, status="Starting download…", completed=0, total=0, error="", active=True)
    try:
        if ollama_models() is None:
            start_ollama()
        req = urllib.request.Request(f"{OLLAMA_URL}/api/pull",
                                     data=json.dumps({"model": name, "name": name, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            for line in r:
                if not line.strip():
                    continue
                ev = json.loads(line)
                if ev.get("error"):
                    raise RuntimeError(ev["error"])
                pull_state["status"] = ev.get("status", "")
                if ev.get("total"):
                    pull_state["total"] = ev["total"]
                    pull_state["completed"] = ev.get("completed", 0)
        pull_state["status"] = "success"
    except Exception as e:
        pull_state["error"] = str(e)[:200]
    finally:
        pull_state["active"] = False
        refresh_provider_availability()


def verify_key(provider, key):
    """Cheap, free check that a key works (lists/looks up models; no tokens are billed)."""
    if provider == "claude":
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("Run `pip3 install anthropic` to use Claude")
        try:
            anthropic.Anthropic(api_key=key, max_retries=0, timeout=15).models.retrieve(CLAUDE_MODEL)
        except anthropic.AuthenticationError:
            raise RuntimeError("That Anthropic key was rejected")
        except anthropic.NotFoundError:
            raise RuntimeError(f"Key works, but it has no access to {CLAUDE_MODEL}")
        except anthropic.APIConnectionError:
            raise RuntimeError("Can't reach Anthropic — check your internet connection")
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"Anthropic error {e.status_code}")
        return
    req = urllib.request.Request("https://api.openai.com/v1/models",
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except urllib.error.HTTPError as e:
        raise RuntimeError("That OpenAI key was rejected" if e.code == 401 else f"OpenAI error {e.code}")
    except urllib.error.URLError:
        raise RuntimeError("Can't reach OpenAI — check your internet connection")


def pair_url():
    return f"http://{get_local_ip()}:{PORT}/#t={ACCESS_TOKEN}"


_qr_cache = {"url": None, "png": b""}


def pair_qr_png():
    """QR code for the pairing link, rendered by the Swift bridge (CoreImage, no extra deps)."""
    url = pair_url()
    if _qr_cache["url"] == url:
        return _qr_cache["png"]
    with tempfile.TemporaryDirectory(prefix="tp-qr-") as td:
        out = Path(td) / "qr.png"
        cp = subprocess.run([str(BRIDGE), "--qr", url, str(out)], capture_output=True, timeout=10)
        if cp.returncode != 0 or not out.exists():
            return b""
        _qr_cache.update(url=url, png=out.read_bytes())
    return _qr_cache["png"]


def setup_status():
    models = ollama_models()
    installed = {m["name"] for m in (models or [])}
    with lock:
        spent = state["spend_usd"]
    return {
        "ram_gb": RAM_GB,
        "recommended": recommended_local_model(),
        "catalog": [dict(m, installed=m["name"] in installed) for m in LOCAL_MODELS],
        "ollama": {
            "installed": ollama_app_installed(),
            "running": models is not None,
            "models": [m for m in (models or []) if m["name"].startswith("qwen")],
            "location": ollama_models_dir(),
        },
        "pull": dict(pull_state),
        "keys": {"claude": bool(get_key("claude")), "openai": bool(get_key("openai"))},
        "settings": {k: SETTINGS[k] for k in ("provider", "local_model", "spend_limit_usd", "setup_complete")},
        "spend_usd": spent,
        "pair_url": pair_url(),
        "claude_model": CLAUDE_MODEL,
        "openai_model": OPENAI_MODEL,
    }


# ---------------------------------------------------------------- providers

def ollama_chat(messages):
    payload = json.dumps({"model": local_model(), "messages": messages, "stream": False, "think": False,
                          "options": {"temperature": 0.25, "num_predict": 260}}).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=45) as r:
        obj = json.loads(r.read().decode())
    text = obj.get("message", {}).get("content", "").strip()
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def claude_call(system_blocks, user_content):
    import anthropic  # pip3 install anthropic

    key = get_key("claude")
    if not key:
        raise RuntimeError("No Anthropic API key — add one in Settings")
    client = anthropic.Anthropic(api_key=key)
    kwargs = dict(model=CLAUDE_MODEL, max_tokens=CLOUD_MAX_OUTPUT, system=system_blocks,
                  output_config={"effort": CLAUDE_EFFORT},
                  messages=[{"role": "user", "content": user_content}])
    try:
        if CLAUDE_MODEL.startswith(CLAUDE_FALLBACK_MODELS):
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                               fallbacks="default", **kwargs)
        else:
            resp = client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        raise RuntimeError("Anthropic key rejected")
    except anthropic.RateLimitError:
        raise RuntimeError("Anthropic rate limit — try again shortly")
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Anthropic HTTP {e.status_code}: {str(e.message)[:120]}")
    except anthropic.APIConnectionError:
        raise RuntimeError("Can't reach Anthropic — check your connection")

    u = resp.usage
    cached = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    written = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    record_spend("claude", int(u.input_tokens or 0) + written, cached, int(u.output_tokens or 0))
    if resp.stop_reason == "refusal":
        return ""
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()


def openai_call(instructions, content, max_output=CLOUD_MAX_OUTPUT):
    key = get_key("openai")
    if not key:
        raise RuntimeError("No OpenAI API key — add one in Settings")
    body = {"model": OPENAI_MODEL, "input": content, "reasoning": {"effort": "low"},
            "max_output_tokens": max_output, "text": {"verbosity": "low"}, "store": False}
    if instructions:
        body["instructions"] = instructions
    req = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=75) as r:
            obj = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OpenAI HTTP {e.code}: {e.read().decode(errors='ignore')[:120]}")
    usage = obj.get("usage", {}) or {}
    total_in = int(usage.get("input_tokens", 0) or 0)
    cached = int((usage.get("input_tokens_details", {}) or {}).get("cached_tokens", 0) or 0)
    record_spend("openai", max(0, total_in - cached), cached, int(usage.get("output_tokens", 0) or 0))
    parts = []
    for item in obj.get("output", []):
        if item.get("type") == "message":
            parts += [c["text"] for c in item.get("content", []) if c.get("type") == "output_text" and c.get("text")]
    return "\n".join(parts).strip()


def build_context_text(transcript):
    docs = load_context()
    with lock:
        screen = state.get("screen_context", "")
    parts = []
    if docs:
        parts.append("USER'S DOCUMENTS:\n" + docs)
    if screen:
        parts.append("CURRENT SCREEN CONTEXT:\n" + screen)
    parts.append("RECENT CONVERSATION:\n" + transcript)
    return docs, screen, "\n\n".join(parts)


def reason(provider, mode, transcript):
    sys_text = system_prompt(mode)
    docs, screen, full = build_context_text(transcript)

    if provider == "local":
        label = ollama_chat([{"role": "system", "content": LOCAL_CLASSIFIER},
                             {"role": "user", "content": full}]).strip().upper()
        label = label.split()[0] if label else "WAIT"
        if label not in ("ANSWER", "COACH", "IMPROVE"):
            return ""
        return ollama_chat([{"role": "system", "content": sys_text},
                            {"role": "user", "content": f"Assistance mode: {label}\n\n{full}"}])

    reserve_check(provider, len(sys_text) + len(full))
    if provider == "claude":
        # Stable prefix (instructions + documents) is cached so repeated calls are cheap.
        system_blocks = [{"type": "text", "text": sys_text}]
        if docs:
            system_blocks.append({"type": "text", "text": "USER'S DOCUMENTS:\n" + docs,
                                  "cache_control": {"type": "ephemeral"}})
        live = ("CURRENT SCREEN CONTEXT:\n" + screen + "\n\n" if screen else "") + \
               "RECENT CONVERSATION:\n" + transcript
        return claude_call(system_blocks, live)
    return openai_call(sys_text, full)


SCREEN_PROMPT = """Analyze this screenshot for a live conversation assistant.
Extract only information useful to the conversation: prompts, slide or exhibit titles,
tables, numbers, labels, constraints, charts, or question text. Be precise with numbers
and units; do not invent unreadable values. Return compact plain text headed
CURRENT SCREEN CONTEXT — it will be reused in later transcript-only calls."""


def analyze_screenshot(provider, image_path):
    if provider not in ("claude", "openai"):
        raise RuntimeError("SCREEN_REQUIRES_CLOUD")
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    reserve_check(provider, 6000)
    if provider == "claude":
        return claude_call([{"type": "text", "text": "You extract facts from screenshots."}], [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": SCREEN_PROMPT},
        ])
    return openai_call(None, [{"role": "user", "content": [
        {"type": "input_text", "text": SCREEN_PROMPT},
        {"type": "input_image", "image_url": "data:image/jpeg;base64," + b64, "detail": "high"},
    ]}], max_output=1500)


def fall_back_to_local(status_key, message):
    with lock:
        state["provider"] = "local"
        state["provider_label"] = provider_label("local")
        state[status_key] = message


def capture_and_analyze_screen():
    with lock:
        provider = state["provider"]
        state["screen_status"] = "Requesting screenshot…"
    try:
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        old = SCREENSHOT_FILE.stat().st_mtime if SCREENSHOT_FILE.exists() else 0
        SCREENSHOT_REQUEST.write_text(str(time.time()))
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if SCREENSHOT_FILE.exists() and SCREENSHOT_FILE.stat().st_mtime > old:
                break
            time.sleep(0.08)
        else:
            raise RuntimeError("No screenshot received from audio bridge")
        with lock:
            state["screen_status"] = f"Reading screen with {provider_label(provider)}…"
        context = analyze_screenshot(provider, SCREENSHOT_FILE)
        if not context:
            raise RuntimeError("No visual context returned")
        with lock:
            state["screen_context"] = context
            state["screen_status"] = "✓ Screen context ready"
            state["suggestion"] = context
    except RuntimeError as e:
        if str(e) == "SPEND_LIMIT":
            fall_back_to_local("screen_status", "Spend limit reached · switched to Local")
        else:
            with lock:
                state["screen_status"] = f"Screenshot failed: {str(e)[:90]}"
    except Exception as e:
        with lock:
            state["screen_status"] = f"Screenshot failed: {str(e)[:90]}"


def maybe_reason(force=False):
    global last_reasoned_text, last_reasoned_at
    with reasoning_lock:
        with lock:
            recent = list(history[-16:])
            provider, mode = state["provider"], state["mode"]
        if not recent:
            return
        latest = recent[-1]
        if len(latest["text"].strip()) < 10:
            return
        signature = f"{provider}:{mode}:{latest['speaker']}:{latest['text'].strip()}"
        if signature == last_reasoned_text:
            return
        if not force and time.time() - last_reasoned_at < 2.5:
            return
        transcript = "\n".join(f'{x["speaker"]}: {x["text"]}' for x in recent)
        last_reasoned_text, last_reasoned_at = signature, time.time()
        try:
            with lock:
                state["reasoning_status"] = ("Thinking locally…" if provider == "local"
                                             else f"Thinking with {provider_label(provider)}…")
            answer = reason(provider, mode, transcript)
            if answer.strip().upper() == "WAIT":
                answer = ""
            with lock:
                if answer:
                    state["suggestion"] = answer
                    state["reasoning_status"] = "Ready"
                else:
                    state["reasoning_status"] = "Listening…"
        except RuntimeError as e:
            if str(e) == "SPEND_LIMIT":
                fall_back_to_local("reasoning_status", "Spend limit reached · switched to Local")
            else:
                with lock:
                    state["reasoning_status"] = f"{provider_label(provider)} unavailable · {str(e)[:90]}"
        except Exception as e:
            with lock:
                state["reasoning_status"] = f"{provider_label(provider)} unavailable · {str(e)[:90]}"


def worker(filename, key):
    path = AUDIO_DIR / filename
    offset = 0
    pending = bytearray()
    while True:
        try:
            if path.exists():
                size = path.stat().st_size
                if size < offset:
                    offset = 0
                    pending.clear()
                if size > offset:
                    with path.open("rb") as f:
                        f.seek(offset)
                        data = f.read(size - offset)
                    offset = size
                    pending.extend(data)
                while len(pending) >= CHUNK_BYTES:
                    chunk = bytes(pending[:CHUNK_BYTES])
                    del pending[:CHUNK_BYTES]
                    text = transcribe(chunk)
                    if text:
                        with lock:
                            state[key] = text
                            state["updated"] = time.time()
                            state["status"] = "Live"
                            history.append({"speaker": "THEM" if key == "them" else "YOU",
                                            "text": text, "time": time.time()})
                            del history[:-16]
                        threading.Thread(target=maybe_reason, daemon=True).start()
            time.sleep(0.15)
        except Exception as e:
            with lock:
                state["status"] = f"{key} error: {e}"
            time.sleep(1)


# ---------------------------------------------------------------- HTTP

class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        p = urlparse(path).path
        if p == "/":
            p = "/index.html"
        return str(PUBLIC / p.lstrip("/"))

    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def authorized(self):
        if self.is_local():
            return True
        return secrets.compare_digest(self.headers.get("X-TP-Token", ""), ACCESS_TOKEN)

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self, limit=25_000_000):
        length = int(self.headers.get("Content-Length", "0"))
        if length > limit:
            raise ValueError("Request too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def snapshot(self):
        with lock:
            s = dict(state)
        s["is_host"] = self.is_local()
        return s

    def do_GET(self):
        route = urlparse(self.path).path
        if route.startswith("/api/"):
            if not self.authorized():
                return self.send_json({"error": "Pairing token required"}, 401)
            if route == "/api/state":
                return self.send_json(self.snapshot())
            if route == "/api/setup":
                if not self.is_local():
                    return self.send_json({"error": "Setup runs on the Mac"}, 403)
                return self.send_json(setup_status())
            return self.send_json({"error": "not found"}, 404)
        if route == "/pair.png":
            png = pair_qr_png() if self.is_local() else b""
            if not png:
                return self.send_json({"error": "QR unavailable"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)
            return
        super().do_GET()

    def do_POST(self):
        global last_reasoned_text, last_reasoned_at
        route = urlparse(self.path).path
        if not self.authorized():
            return self.send_json({"error": "Pairing token required"}, 401)
        try:
            data = self.read_json()
        except Exception as e:
            return self.send_json({"error": str(e)}, 400)

        if route == "/api/screenshot":
            with lock:
                if state["provider"] == "local":
                    return self.send_json({"error": "Screen reading needs Claude or ChatGPT"}, 409)
                state["screen_status"] = "Screenshot requested…"
            threading.Thread(target=capture_and_analyze_screen, daemon=True).start()
            return self.send_json(self.snapshot())

        if route.startswith("/api/setup/") or route == "/api/spend/reset":
            if not self.is_local():
                return self.send_json({"error": "Setup runs on the Mac"}, 403)
            try:
                return self.handle_setup(route, data)
            except RuntimeError as e:
                return self.send_json({"error": str(e)}, 400)

        if route == "/api/provider":
            requested = str(data.get("provider", "")).lower()
            if requested not in PROVIDERS:
                return self.send_json({"error": f"provider must be one of {', '.join(PROVIDERS)}"}, 400)
            avail = refresh_provider_availability()
            if requested == "local" and not avail["local"]:
                return self.send_json({"error": f"{local_model()} isn't downloaded — finish setup on the Mac"}, 409)
            if requested != "local":
                if not avail.get(requested):
                    return self.send_json({"error": "Add an API key in Settings first"}, 409)
                with lock:
                    if state["spend_usd"] >= spend_limit():
                        return self.send_json({"error": "Spend limit reached"}, 409)
            with lock:
                state["provider"] = requested
                state["provider_label"] = provider_label(requested)
                state["reasoning_status"] = f"{provider_label(requested)} selected"
            SETTINGS["provider"] = requested
            save_settings()
        elif route == "/api/mode":
            requested = str(data.get("mode", "")).lower()
            if requested not in MODES:
                return self.send_json({"error": "unknown mode"}, 400)
            with lock:
                state["mode"] = requested
                state["reasoning_status"] = f"{MODES[requested]['label']} mode"
        elif route == "/api/context":
            name = safe_filename(data.get("name"))
            if not name or Path(name).suffix.lower() not in CONTEXT_EXTS:
                return self.send_json({"error": "Unsupported file type"}, 400)
            try:
                raw = base64.b64decode(data.get("data", ""), validate=True)
            except Exception:
                return self.send_json({"error": "Bad file data"}, 400)
            CONTEXT_DIR.mkdir(exist_ok=True)
            (CONTEXT_DIR / name).write_bytes(raw)
            load_context()
            return self.send_json(self.snapshot())
        elif route == "/api/context/delete":
            name = safe_filename(data.get("name"))
            target = CONTEXT_DIR / name
            if name and target.is_file():
                target.unlink()
            load_context()
            return self.send_json(self.snapshot())
        elif route == "/api/key":
            # Keys are only accepted from the Mac itself, never over the network.
            if not self.is_local():
                return self.send_json({"error": "Add API keys from the Mac running ThinkingPartner"}, 403)
            provider = str(data.get("provider", "")).lower()
            key = str(data.get("key", "")).strip()
            if provider not in ("claude", "openai") or len(key) < 20:
                return self.send_json({"error": "That doesn't look like a complete API key"}, 400)
            try:
                verify_key(provider, key)
                store_key(provider, key)
            except RuntimeError as e:
                return self.send_json({"error": str(e)}, 400)
            refresh_provider_availability()
            return self.send_json(self.snapshot())
        else:
            return self.send_json({"error": "not found"}, 404)

        last_reasoned_text, last_reasoned_at = "", 0.0
        threading.Thread(target=maybe_reason, kwargs={"force": True}, daemon=True).start()
        return self.send_json(self.snapshot())

    def handle_setup(self, route, data):
        if route == "/api/setup/ollama/start":
            start_ollama()
        elif route == "/api/setup/pull":
            name = str(data.get("model", ""))
            if name not in {m["name"] for m in LOCAL_MODELS}:
                raise RuntimeError("Unknown model")
            if pull_state["active"]:
                raise RuntimeError(f"Already downloading {pull_state['model']}")
            threading.Thread(target=pull_model, args=(name,), daemon=True).start()
            time.sleep(0.2)
        elif route == "/api/setup/local-model":
            name = str(data.get("model", ""))
            if not name.startswith("qwen"):
                raise RuntimeError("Pick a Qwen model")
            SETTINGS["local_model"] = name
            save_settings()
            refresh_provider_availability()
        elif route == "/api/setup/spend-limit":
            limit = float(data.get("limit", 0))
            if not 0 < limit <= 10000:
                raise RuntimeError("Enter a limit between $1 and $10,000")
            SETTINGS["spend_limit_usd"] = round(limit, 2)
            save_settings()
            with lock:
                state["spend_limit_usd"] = spend_limit()
        elif route == "/api/spend/reset":
            with lock:
                state["spend_usd"] = 0.0
                save_spend(0.0)
        elif route == "/api/setup/finish":
            provider = str(data.get("provider", SETTINGS["provider"]))
            avail = refresh_provider_availability()
            if provider not in PROVIDERS or not avail.get(provider):
                raise RuntimeError("Finish choosing a model first")
            SETTINGS.update(setup_complete=True, provider=provider)
            save_settings()
            with lock:
                state.update(setup_complete=True, provider=provider, provider_label=provider_label(provider))
        elif route == "/api/setup/restart":
            SETTINGS["setup_complete"] = False
            save_settings()
            with lock:
                state["setup_complete"] = False
        else:
            return self.send_json({"error": "not found"}, 404)
        return self.send_json(setup_status())

    def log_message(self, fmt, *args):
        pass


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def main():
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    if not WHISPER.exists():
        raise SystemExit(f"Missing whisper-cli: {WHISPER}  (run ./scripts/setup.sh)")
    if not WHISPER_MODEL.exists():
        raise SystemExit(f"Missing Whisper model: {WHISPER_MODEL}  (run ./scripts/setup.sh)")
    load_context()
    refresh_provider_availability()
    threading.Thread(target=worker, args=("system.pcm", "them"), daemon=True).start()
    threading.Thread(target=worker, args=("mic.pcm", "you"), daemon=True).start()
    with lock:
        state["status"] = "Listening…"
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"\n{APP_NAME} is running.", flush=True)
    if not SETTINGS["setup_complete"]:
        print(f"  First time? Finish setup in your browser: http://localhost:{PORT}", flush=True)
    else:
        print(f"  This Mac:      http://localhost:{PORT}", flush=True)
        print(f"  iPad / phone:  {pair_url()}", flush=True)
    print("", flush=True)
    if os.environ.get("TP_OPEN_BROWSER", "1") == "1":
        subprocess.Popen(["open", f"http://localhost:{PORT}"])
    server.serve_forever()


if __name__ == "__main__":
    main()
