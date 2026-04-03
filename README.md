# Whisper Telegram Bot

A personal Telegram bot that transcribes voice messages, audio files, and videos using Whisper, running entirely on your local machine. No cloud APIs — everything stays on your hardware.

## How it works

```
You (Telegram) ──> Bot receives audio/video ──> Whisper transcribes locally ──> You get a .md file back
```

1. Send a voice message, audio file, or video to the bot
2. Whisper (large-v3, int8 quantization) transcribes it on your machine
3. Bot sends back a clean markdown file with the transcription

## Architecture

```
Telegram ──> bot.py (polling) ──> transcriber.py ──> faster-whisper (large-v3)
                │                       │
                │                       └── Returns: text, language, duration
                │
                ├── Generates .md file with metadata header
                ├── Sends .md back to Telegram
                └── Cleans up temp files
```

### File responsibilities

| File | What it does |
|------|-------------|
| `config.py` | Loads `.env`, defines all constants (model, paths, limits, supported formats). Creates `tmp/` directory on import. |
| `transcriber.py` | Loads the Whisper model once (lazy singleton). Exposes `transcribe_audio(path)` which returns a `TranscriptionResult` dataclass (text, language, duration). Synchronous — called via `asyncio.to_thread()`. |
| `bot.py` | All Telegram logic. Handlers for voice, audio, video, video notes, and documents. Shared `_process_audio()` does: size check → download → transcribe → generate markdown → send file → cleanup. |

## Requirements

- macOS with Apple Silicon (tested on Mac Mini M4, 16GB RAM)
- Python 3.11+ (tested on 3.14)
- ffmpeg (for audio/video decoding)
- ~6-8GB free RAM (for the Whisper large-v3 model)
- ~3GB disk space (for the cached model in `~/.cache/huggingface/`)

## Setup

### 1. Install ffmpeg

```bash
brew install ffmpeg
```

### 2. Create a Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a name and username for your bot
4. Copy the bot token

### 3. Configure the bot

```bash
cd "Whisper Telegram Bot"

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create .env file with your bot token
echo 'BOT_TOKEN=your-token-here' > .env
```

### 4. Run the bot

```bash
source venv/bin/activate
python bot.py
```

The first time you send audio, the Whisper model (~3GB) will be downloaded and cached. This only happens once.

## Supported input types

Telegram sends media in different message types depending on how the user sends it:

| How you send it | Telegram type | Formats |
|---|---|---|
| Record a voice message | `voice` | Always `.ogg` Opus |
| Attach via audio picker | `audio` | mp3, m4a, wav, ogg |
| Send a video | `video` | mp4, etc. (audio track extracted by ffmpeg) |
| Record a video note (circle) | `video_note` | mp4 |
| Drag-and-drop a file | `document` | Any supported audio extension |

Maximum file size: 20MB (Telegram Bot API limit — no workaround in standard mode).

## Running permanently with launchd

Instead of keeping a terminal open, you can use macOS's built-in service manager to run the bot in the background. It will:
- Start automatically when you log in
- Restart automatically if it crashes
- Keep running even if you close VS Code or Terminal

The plist file lives at:
```
~/Library/LaunchAgents/com.whisper.telegram-bot.plist
```

### Commands

| What you want | Command |
|---|---|
| Start the bot | `launchctl load ~/Library/LaunchAgents/com.whisper.telegram-bot.plist` |
| Stop the bot | `launchctl unload ~/Library/LaunchAgents/com.whisper.telegram-bot.plist` |
| Check if it's running | `launchctl list \| grep whisper` |
| View logs | `cat ~/Library/Logs/whisper-bot.log` |
| View error logs | `cat ~/Library/Logs/whisper-bot-error.log` |

**Important:** Before loading, make sure no bot process is already running (see "409 Conflict" in Gotchas below).

## Output

The bot sends back a markdown file named `transcription_YYYY-MM-DD_HHMMSS.md` containing:

- Metadata header (date, duration, language, model)
- Clean transcribed text with paragraph breaks

## Performance

On Mac Mini M4 with the large-v3 model (int8):
- Transcription speed is roughly realtime (1 min audio ≈ 30-60 sec processing)
- CTranslate2 uses CPU on macOS (Metal GPU not supported yet)
- Only one transcription runs at a time to stay within 16GB RAM

---

## Lessons Learned & Gotchas

Things we ran into during development that are worth knowing.

### Python 3.14 broke the event loop (CRITICAL)

**What happened:** Python 3.14 removed automatic event loop creation. When `python-telegram-bot` calls `asyncio.get_event_loop()` internally, it now throws:
```
RuntimeError: There is no current event loop in thread 'MainThread'
```

**The fix:** Before calling `main()`, manually create and set a loop:
```python
asyncio.set_event_loop(asyncio.new_event_loop())
```

This is at the bottom of `bot.py`. If `python-telegram-bot` fixes this upstream, you can remove the workaround.

**The lesson:** When you use the latest version of Python, you may hit breaking changes that libraries haven't caught up with yet. Check release notes if something suddenly stops working after an upgrade.

### Telegram 409 Conflict error

**What happened:** During development, we got:
```
telegram.error.Conflict: terminated by other getUpdates request
```

**Why:** Telegram only allows ONE polling connection per bot token. If a previous `python bot.py` process is still running in the background, the new one can't connect.

**How to fix:**
```bash
# Find and kill ALL bot processes
ps aux | grep "[b]ot.py" | awk '{print $2}' | xargs kill -9

# Clear Telegram's pending state
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates?offset=-1&timeout=1"

# Wait a few seconds, then restart
sleep 5
python bot.py
```

**The lesson:** `pkill -f "python bot.py"` didn't work on macOS because the process was running under the full Python framework path. Always use `ps aux | grep bot.py` to verify processes are actually killed.

### Telegram sends audio in 3+ different ways

**What happened:** We built a handler for voice messages, tested it, it worked. Then sent an mp3 file — nothing happened. Then drag-and-dropped a file — nothing happened again.

**Why:** Telegram uses completely different message types depending on HOW the user sends audio:
- Voice recording → `update.message.voice`
- Audio picker → `update.message.audio`
- Drag-and-drop → `update.message.document`
- Video → `update.message.video`
- Video note (circle) → `update.message.video_note`

Each one needs its own handler with its own filter. They all call the same `_process_audio()` function underneath.

**The lesson:** Don't assume one handler covers all cases. Test every way a user might send something.

### ffmpeg is a hidden dependency

**What happened:** `pip install faster-whisper` succeeded, but transcription failed with a cryptic error.

**Why:** faster-whisper uses ffmpeg under the hood to decode audio files (ogg, mp3, m4a, mp4, etc.), but ffmpeg isn't a Python package — it's a system tool that needs to be installed separately.

**The fix:** `brew install ffmpeg`

**The lesson:** Some Python packages depend on system-level tools that aren't listed in `requirements.txt`. If something fails with a confusing error after a clean install, check if there's an external dependency.

### Video transcription works because of ffmpeg

**What happened:** We expected to need special video handling, but videos just worked when passed through the same `_process_audio()` pipeline.

**Why:** ffmpeg (which faster-whisper uses internally) automatically extracts the audio track from video files. It doesn't care if the input is `.mp3` or `.mp4` — it just finds the audio stream and decodes it.

**The lesson:** Before building a complex solution (like a separate video-to-audio extraction step), test the simple path first. The tools you're already using might handle it.

### First run downloads 3GB silently

The first time `transcriber.py` loads the model, it downloads `large-v3` from Hugging Face to `~/.cache/huggingface/`. This takes several minutes and there's no obvious progress indicator in the bot.

To pre-download without running the bot:
```bash
source venv/bin/activate
python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', device='auto', compute_type='int8')"
```

### Temp file cleanup

Audio downloads and markdown files are written to `./tmp/`. The `finally` block in `_process_audio()` cleans them up. If the bot crashes mid-transcription, orphaned files may remain:
```bash
rm -rf tmp/*
```

### The document handler silently ignores non-audio files

This is intentional. When someone sends a PDF or image, the bot does nothing instead of replying "unsupported format." This avoids spam in group chats or when you accidentally send the wrong file.

---

## Key design decisions

| Decision | Why |
|---|---|
| **faster-whisper** over whisper.cpp | Pure Python integration via pip. No compiling C++, no subprocess calls. |
| **large-v3 + int8** | Best accuracy. int8 cuts RAM from ~10-12GB to ~6-8GB, fits in 16GB. |
| **asyncio.to_thread()** | Whisper is CPU-bound (30-60 sec). Without this, the bot freezes during transcription. Works because Whisper's C code releases the GIL. |
| **Semaphore(1)** | Prevents two transcriptions from running simultaneously and using ~12-16GB RAM. |
| **Polling, not webhooks** | Webhooks need a public URL with HTTPS. Polling works behind any home network with zero config. ~1-2 sec latency, which doesn't matter for a personal bot. |
| **Lazy model loading** | Bot starts instantly. Model loads on first request. Lets you verify Telegram connectivity before the heavy 6-8GB model loads. |
| **launchd, not Docker** | Native macOS, zero overhead, uses the existing venv and model cache. Docker would need its own copy of everything inside a container. |

---

## Configuration (config.py)

| Constant | Default | What it controls |
|----------|---------|-----------------|
| `BOT_TOKEN` | from `.env` | Telegram bot token |
| `WHISPER_MODEL` | `"large-v3"` | Model size. Options: tiny, base, small, medium, large-v3 |
| `WHISPER_COMPUTE_TYPE` | `"int8"` | Quantization. Options: float32, float16, int8 |
| `MAX_AUDIO_SIZE_MB` | `20` | Max file size in MB (Telegram's limit) |
| `SUPPORTED_EXTENSIONS` | `.ogg .mp3 .wav .m4a` | Accepted audio formats for document uploads |

## Known limitations

- **CPU only on macOS** — CTranslate2 doesn't support Metal GPU yet
- **20MB file limit** — Telegram Bot API constraint
- **Single transcription at a time** — by design, to prevent OOM on 16GB
- **No language selection** — auto-detects language, no way to override via chat
- **No speaker diarization** — Whisper doesn't identify who is speaking

## Project structure

```
.env                 # Bot token (never committed)
.gitignore
config.py            # Configuration constants
transcriber.py       # Whisper model loading and transcription
bot.py               # Telegram bot handlers and orchestration
requirements.txt     # Python dependencies
README.md            # This file
CLAUDE.md            # Context for Claude Code sessions
```
