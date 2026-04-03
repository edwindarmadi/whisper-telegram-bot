# Whisper Telegram Bot

A personal Telegram bot that transcribes voice messages and audio files using Whisper, running entirely on your local machine. No cloud APIs — everything stays on your hardware.

## How it works

```
You (Telegram) ──> Bot receives audio ──> Whisper transcribes locally ──> You get a .md file back
```

1. Send a voice message or audio file to the bot
2. Whisper (large-v3, int8 quantization) transcribes it on your machine
3. Bot sends back a clean markdown file with the transcription

## Requirements

- macOS with Apple Silicon (tested on Mac Mini M4, 16GB RAM)
- Python 3.11+ (tested on 3.14)
- ffmpeg (for audio decoding)
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
# Clone or navigate to the project
cd "Whisper Telegram Bot"

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Add your bot token
# Open .env and replace the placeholder with your actual token
```

### 4. Run the bot

```bash
source venv/bin/activate
python bot.py
```

The first time you send audio, the Whisper model (~3GB) will be downloaded and cached. This only happens once.

## Supported formats

- Voice messages (OGG Opus — Telegram's default)
- MP3
- WAV
- M4A
- OGG

Maximum file size: 20MB (Telegram's limit)

## Output

The bot sends back a markdown file named `transcription_YYYY-MM-DD_HHMMSS.md` containing:

- Metadata header (date, duration, language, model)
- Clean transcribed text with paragraph breaks

## Performance

On Mac Mini M4 with the large-v3 model (int8):
- Transcription speed is roughly realtime (1 min audio takes ~30-60 seconds)
- CTranslate2 uses CPU on macOS (Metal GPU not supported yet)
- Only one transcription runs at a time to stay within 16GB RAM

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
