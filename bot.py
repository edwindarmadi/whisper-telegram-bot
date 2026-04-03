import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from config import BOT_TOKEN, TMP_DIR, MAX_AUDIO_SIZE_MB, SUPPORTED_EXTENSIONS, WHISPER_MODEL, WHISPER_COMPUTE_TYPE
from transcriber import transcribe_audio

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Only 1 transcription at a time to avoid running out of memory
_transcription_lock = asyncio.Semaphore(1)


async def _process_audio(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         tg_file_obj, file_size: int, extension: str) -> None:
    """Shared logic: check size, download, transcribe, reply."""
    file_size_mb = file_size / (1024 * 1024)

    if file_size_mb > MAX_AUDIO_SIZE_MB:
        await update.message.reply_text(
            f"Audio is too large ({file_size_mb:.1f}MB). Telegram limits downloads to {MAX_AUDIO_SIZE_MB}MB."
        )
        return

    # Check if another transcription is running
    queued = _transcription_lock.locked()
    if queued:
        status_msg = await update.message.reply_text("Another transcription is in progress. Yours is queued...")
    else:
        status_msg = await update.message.reply_text("Transcribing your audio...")

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    audio_path = TMP_DIR / f"{update.message.message_id}_{int(time.time())}{extension}"
    md_path = None

    try:
        tg_file = await tg_file_obj.get_file()
        await tg_file.download_to_drive(audio_path)
        logger.info(f"Downloaded audio: {audio_path.name} ({file_size_mb:.1f}MB)")

        async with _transcription_lock:
            if queued:
                await status_msg.edit_text("Transcribing your audio...")
            result = await asyncio.to_thread(transcribe_audio, audio_path)

        duration_min = int(result.duration // 60)
        duration_sec = int(result.duration % 60)
        now = datetime.now()
        logger.info(f"Transcribed: {result.language}, {duration_min}m {duration_sec:02d}s")

        markdown = (
            f"# Transcription\n\n"
            f"**Date:** {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"**Audio Duration:** {duration_min}m {duration_sec:02d}s\n"
            f"**Language:** {result.language} (auto-detected)\n"
            f"**Model:** {WHISPER_MODEL} ({WHISPER_COMPUTE_TYPE})\n\n"
            f"---\n\n"
            f"{result.text}\n"
        )

        md_filename = f"transcription_{now.strftime('%Y-%m-%d_%H%M%S')}.md"
        md_path = TMP_DIR / md_filename
        md_path.write_text(markdown, encoding="utf-8")

        await update.message.reply_document(
            document=open(md_path, "rb"),
            filename=md_filename,
            caption=f"{result.language} | {duration_min}m {duration_sec:02d}s",
        )
        await status_msg.delete()
    except Exception as e:
        logger.error(f"Transcription failed: {e}", exc_info=True)
        await status_msg.edit_text("Sorry, transcription failed. Please try again.")
    finally:
        audio_path.unlink(missing_ok=True)
        if md_path:
            md_path.unlink(missing_ok=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hi! I'm your Whisper transcription bot.\n\n"
        "Send me a voice message or audio file and I'll transcribe it for you.\n\n"
        "Supported formats: ogg, mp3, wav, m4a"
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    voice = update.message.voice
    await _process_audio(update, context, voice, voice.file_size, ".ogg")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    audio = update.message.audio
    file_name = audio.file_name or "audio.mp3"
    ext = Path(file_name).suffix.lower()

    if ext not in SUPPORTED_EXTENSIONS:
        await update.message.reply_text(
            f"Unsupported format: {ext}\n"
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
        return

    await _process_audio(update, context, audio, audio.file_size, ext)


async def handle_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    video_note = update.message.video_note
    await _process_audio(update, context, video_note, video_note.file_size, ".mp4")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    video = update.message.video
    await _process_audio(update, context, video, video.file_size, ".mp4")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    file_name = doc.file_name or ""
    ext = Path(file_name).suffix.lower()

    if ext not in SUPPORTED_EXTENSIONS:
        return  # Silently ignore non-audio documents

    await _process_audio(update, context, doc, doc.file_size, ext)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Unhandled exception: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.message:
        await update.message.reply_text("Something went wrong. Please try again.")


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "your-telegram-bot-token-here":
        print("ERROR: Set your bot token in .env file")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_video_note))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_error_handler(error_handler)

    logger.info("Bot started — polling for messages...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    # Python 3.14 removed auto-creation of event loops — create one for run_polling()
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
