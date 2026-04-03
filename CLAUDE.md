# Whisper Telegram Bot

## What this is

A personal Telegram bot that transcribes voice messages, audio files, and videos using faster-whisper (large-v3, int8) running locally on a Mac Mini M4 (16GB RAM). No cloud APIs. No LLM cleanup. Just Whisper → markdown file → back to Telegram.

---

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
| `transcriber.py` | Loads the Whisper model once (lazy singleton). Exposes `transcribe_audio(path)` which returns a `TranscriptionResult` dataclass (text, language, duration). Synchronous — meant to be called via `asyncio.to_thread()`. |
| `bot.py` | All Telegram logic. Handlers for voice, audio, video, video notes, and documents. Shared `_process_audio()` does: size check → download → transcribe → generate markdown → send file → cleanup. Also has `/start` command and global error handler. |

### How media arrives in Telegram (5 different ways)

This tripped us up during development. Telegram sends media in **five different message types** depending on how the user sends it:

| How user sends it | Telegram message type | Filter in code | Format |
|---|---|---|---|
| Record a voice message | `voice` | `filters.VOICE` | Always `.ogg` Opus |
| Attach via audio picker | `audio` | `filters.AUDIO` | mp3, m4a, etc. |
| Send a video | `video` | `filters.VIDEO` | mp4, etc. |
| Record a video note (circle) | `video_note` | `filters.VIDEO_NOTE` | mp4 |
| Drag-and-drop a file | `document` | `filters.Document.ALL` | Any extension |

Each needs its own handler because the Telegram API object is different (`update.message.voice` vs `.audio` vs `.video` vs `.video_note` vs `.document`), but they all call the same `_process_audio()` function.

Video files work because ffmpeg (used by faster-whisper under the hood) automatically extracts the audio track — no separate conversion step needed.

---

## Key technical decisions and WHY

### faster-whisper over whisper.cpp
- **Why:** Pure Python integration via pip. No compiling C++, no ctypes bindings, no subprocess calls. Import it, call it, done.
- **Trade-off:** whisper.cpp has better Metal GPU support, but CTranslate2 (faster-whisper's backend) doesn't support Metal on macOS yet. Both end up using CPU anyway.

### large-v3 model with int8 quantization
- **Why:** Best transcription accuracy. int8 quantization cuts RAM from ~10-12GB to ~6-8GB, which fits comfortably in 16GB with room for macOS and the bot.
- **If you have less RAM:** Use `medium` model or `int8_float16` compute type in `config.py`.

### asyncio.to_thread() for Whisper calls
- **Why:** Whisper transcription is CPU-bound and takes 30-60 seconds. If we ran it directly in the async handler, the bot would freeze — it couldn't receive new messages, respond to `/start`, or do anything until transcription finishes. `to_thread()` runs Whisper in a separate thread while the bot stays responsive.
- **Why not multiprocessing?** Whisper's C/C++ code releases Python's GIL (Global Interpreter Lock), so a thread actually gets true parallelism here. Multiprocessing would work but adds complexity (serialization, shared state) for no benefit.

### Semaphore(1) for concurrency control
- **Why:** The large-v3 model uses ~6-8GB RAM. Running two transcriptions simultaneously would use ~12-16GB and likely crash or severely slow down macOS. The semaphore ensures only one transcription runs at a time, queuing others.
- **User experience:** When queued, the bot tells the user "Another transcription is in progress. Yours is queued..."

### Polling mode (not webhooks)
- **Why:** Webhooks require a public URL with HTTPS — that means port forwarding, a domain name, and a TLS certificate. Polling just makes outbound HTTPS requests to Telegram's servers, so it works behind any home network/NAT with zero configuration.
- **Trade-off:** Polling has ~1-2 second latency (the bot checks for new messages periodically). Webhooks are instant. For a personal bot, this doesn't matter.

### Lazy model loading (singleton pattern)
- **Why:** The Whisper model takes several seconds to load and ~6-8GB RAM. Loading at import time would slow down bot startup and make it impossible to test Telegram connectivity without the model. Loading on first request means the bot starts instantly, and you can verify the connection works before the heavy model loads.
- **Pattern:** Module-level `_model` variable in `transcriber.py`, initialized on first call to `get_model()`.

---

## Gotchas and mistakes to avoid

### Python 3.14 event loop issue (CRITICAL)
Python 3.14 removed automatic event loop creation. `python-telegram-bot`'s `run_polling()` calls `asyncio.get_event_loop()` internally, which now throws:
```
RuntimeError: There is no current event loop in thread 'MainThread'
```
**Fix:** Before calling `main()`, create and set a loop:
```python
asyncio.set_event_loop(asyncio.new_event_loop())
```
This is in `bot.py` at the bottom. If you upgrade `python-telegram-bot`, check if they've fixed this upstream and remove the workaround.

### Telegram 409 Conflict error
If you see:
```
telegram.error.Conflict: terminated by other getUpdates request
```
This means another bot process is still running and holding the polling connection. Telegram only allows **one** polling connection per bot token at a time.

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

**Why this happened to us:** During development, background processes from previous runs weren't fully killed. `pkill -f "python bot.py"` didn't catch them because macOS was running them under the full Python framework path. Always use `ps aux | grep bot.py` to verify.

### ffmpeg is required but not obvious
faster-whisper uses ffmpeg under the hood to decode audio AND video files (ogg, mp3, m4a, mp4, etc.). Without it, transcription fails with a cryptic error. Install via:
```bash
brew install ffmpeg
```

### First run downloads ~3GB
The first time `transcriber.py` loads the model, it downloads `large-v3` from Hugging Face to `~/.cache/huggingface/`. This takes several minutes on a normal connection. After that, it's cached and loads in seconds.

To pre-download without running the bot:
```bash
source venv/bin/activate
python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', device='auto', compute_type='int8')"
```

### Temp file cleanup
Audio downloads and markdown files are written to `./tmp/`. The `finally` block in `_process_audio()` cleans them up. If the bot crashes mid-transcription, orphaned files may remain. They're harmless but you can clean up with:
```bash
rm -rf tmp/*
```

### Telegram's 20MB download limit
Telegram Bot API limits file downloads to 20MB. The bot checks `file.file_size` before downloading and rejects oversized files with a friendly message. This is a Telegram limitation — there's no workaround without using the Bot API's local server mode.

### The document handler silently ignores non-audio files
When someone sends a non-audio document (PDF, image, etc.), the `handle_document` function returns silently instead of sending an error. This is intentional — in a group chat or if you accidentally send a screenshot, you don't want the bot spamming "unsupported format" for every non-audio file.

---

## How to run

```bash
cd ~/Claude\ Code\ Projects/Whisper\ Telegram\ Bot
source venv/bin/activate
python bot.py
```

The bot runs until you hit Ctrl+C or close the terminal.

### Running permanently (launchd — already set up)

The bot runs as a launchd service, managed by macOS. It auto-starts on login and auto-restarts on crash.

**Plist location:** `~/Library/LaunchAgents/com.whisper.telegram-bot.plist`

| What you want | Command |
|---|---|
| Start the bot | `launchctl load ~/Library/LaunchAgents/com.whisper.telegram-bot.plist` |
| Stop the bot | `launchctl unload ~/Library/LaunchAgents/com.whisper.telegram-bot.plist` |
| Check if running | `launchctl list \| grep whisper` |
| View logs | `cat ~/Library/Logs/whisper-bot.log` |
| View error logs | `cat ~/Library/Logs/whisper-bot-error.log` |

**Important:** Before loading, make sure no other bot process is running or you'll get the 409 Conflict error.

---

## How to test

1. **Basic connectivity:** Send `/start` — should get welcome message
2. **Voice message:** Record and send a voice message — should get `.md` file back
3. **Audio file:** Send an mp3/wav/m4a — should get `.md` file back
4. **Video:** Send a video — should extract audio and get `.md` file back
5. **Video note:** Record a circle video note — should get `.md` file back
6. **Document upload:** Drag-and-drop an audio file — should get `.md` file back
7. **Oversized file:** Send a file > 20MB — should get a friendly error
8. **Unsupported format:** Send audio as "audio" with a weird extension — should get format list
9. **Non-audio document:** Send a PDF — should be silently ignored
10. **Concurrent requests:** Send two voice messages quickly — second should say "queued"

---

## Dependencies

| Package | Purpose | Install method |
|---------|---------|---------------|
| python-telegram-bot | Telegram Bot API wrapper (async) | pip (requirements.txt) |
| faster-whisper | Whisper transcription via CTranslate2 | pip (requirements.txt) |
| python-dotenv | Load .env file | pip (requirements.txt) |
| ffmpeg | Audio decoding (used by faster-whisper) | `brew install ffmpeg` |

---

## Configuration reference (config.py)

| Constant | Default | What it controls |
|----------|---------|-----------------|
| `BOT_TOKEN` | from `.env` | Telegram bot token |
| `WHISPER_MODEL` | `"large-v3"` | Whisper model size. Options: tiny, base, small, medium, large-v3 |
| `WHISPER_COMPUTE_TYPE` | `"int8"` | Quantization. Options: float32, float16, int8. Lower = less RAM but slightly less accurate |
| `MAX_AUDIO_SIZE_MB` | `20` | Max audio file size in MB |
| `SUPPORTED_EXTENSIONS` | `.ogg .mp3 .wav .m4a` | Accepted audio formats (for document uploads only — voice, video, and video_note bypass this check) |
| `TMP_DIR` | `./tmp` | Where temp files are stored during processing |

---

## Known limitations

- **CPU only on macOS** — CTranslate2 doesn't support Metal GPU acceleration yet. Transcription is ~realtime speed (1 min audio ≈ 30-60 sec processing).
- **20MB file limit** — Telegram Bot API constraint. No workaround in standard mode.
- **Single transcription at a time** — by design, to prevent OOM on 16GB RAM.
- **No language selection** — auto-detects language. If auto-detection is wrong, there's no way to override via the chat interface (would need a `/lang` command).
- **No speaker diarization** — Whisper doesn't identify who is speaking. All speech is one continuous transcript.
