import asyncio
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from config import BOT_TOKEN, TMP_DIR, MAX_AUDIO_SIZE_MB, SUPPORTED_EXTENSIONS, WHISPER_MODEL, WHISPER_COMPUTE_TYPE, OBSIDIAN_TRANSCRIPTIONS
from transcriber import transcribe_audio
from speaker_id import (
    enroll_speaker, load_enrolled_speakers, identify_speakers,
    list_enrolled_speakers, remove_speaker,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Only 1 transcription at a time to avoid running out of memory
_transcription_lock = asyncio.Semaphore(1)


def _is_enrolling(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if the user is currently in enrollment mode."""
    return bool(context.user_data.get("enrolling_as"))


async def _handle_enrollment(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             tg_file_obj, extension: str) -> None:
    """Handle an audio message during enrollment mode."""
    name = context.user_data["enrolling_as"]
    audio_path = TMP_DIR / f"enroll_{update.message.message_id}_{int(time.time())}{extension}"

    try:
        tg_file = await tg_file_obj.get_file()
        await tg_file.download_to_drive(audio_path)

        result_msg = await asyncio.to_thread(enroll_speaker, name, audio_path)
        await update.message.reply_text(result_msg)
    except Exception as e:
        logger.error(f"Enrollment failed: {e}", exc_info=True)
        await update.message.reply_text("Enrollment failed. Please try again.")
    finally:
        audio_path.unlink(missing_ok=True)


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

            # Run speaker identification if speakers are enrolled
            enrolled = load_enrolled_speakers()
            if enrolled:
                await status_msg.edit_text("Transcribing your audio... identifying speakers...")
                result.segments = await asyncio.to_thread(
                    identify_speakers, audio_path, result.segments, enrolled
                )

        duration_min = int(result.duration // 60)
        duration_sec = int(result.duration % 60)
        now = datetime.now()
        logger.info(f"Transcribed: {result.language}, {duration_min}m {duration_sec:02d}s")

        # Build transcript body with or without speaker labels
        has_speakers = any(seg.speaker for seg in result.segments)
        if has_speakers:
            lines = []
            current_speaker = None
            current_texts = []
            for seg in result.segments:
                speaker = seg.speaker or "Unknown"
                if speaker != current_speaker:
                    if current_texts:
                        lines.append(f"**[{current_speaker}]** {' '.join(current_texts)}")
                    current_speaker = speaker
                    current_texts = [seg.text]
                else:
                    current_texts.append(seg.text)
            if current_texts:
                lines.append(f"**[{current_speaker}]** {' '.join(current_texts)}")
            transcript_body = "\n\n".join(lines)
        else:
            transcript_body = result.text

        markdown = (
            f"# Transcription\n\n"
            f"**Date:** {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"**Audio Duration:** {duration_min}m {duration_sec:02d}s\n"
            f"**Language:** {result.language} (auto-detected)\n"
            f"**Model:** {WHISPER_MODEL} ({WHISPER_COMPUTE_TYPE})\n\n"
            f"---\n\n"
            f"{transcript_body}\n"
        )

        md_filename = f"Transcription - {now.strftime('%b %-d, %Y %-I.%M %p')}.md"
        md_path = TMP_DIR / md_filename
        md_path.write_text(markdown, encoding="utf-8")

        await update.message.reply_document(
            document=open(md_path, "rb"),
            filename=md_filename,
            caption=f"{result.language} | {duration_min}m {duration_sec:02d}s",
        )
        shutil.copy2(md_path, OBSIDIAN_TRANSCRIPTIONS / md_filename)
        logger.info(f"Saved to Obsidian: {md_filename}")
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
        "Supported formats: ogg, mp3, wav, m4a\n\n"
        "Speaker identification commands:\n"
        "/enroll <Name> — enroll a speaker's voice\n"
        "/speakers — list enrolled speakers\n"
        "/unenroll <Name> — remove a speaker"
    )


async def handle_enroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start enrollment mode for a speaker."""
    if not context.args:
        await update.message.reply_text("Usage: /enroll <Name>\nExample: /enroll Edwin")
        return

    name = " ".join(context.args)
    context.user_data["enrolling_as"] = name
    await update.message.reply_text(
        f"Enrollment mode for {name}.\n\n"
        f"Send me audio clips of {name} speaking (5-30 seconds of clear speech each).\n"
        f"Send /done when finished."
    )


async def handle_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exit enrollment mode."""
    name = context.user_data.pop("enrolling_as", None)
    if name:
        await update.message.reply_text(f"Finished enrolling {name}.")
    else:
        await update.message.reply_text("No enrollment in progress.")


async def handle_speakers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all enrolled speakers."""
    speakers = list_enrolled_speakers()
    if speakers:
        speaker_list = "\n".join(f"- {name}" for name in speakers)
        await update.message.reply_text(f"Enrolled speakers:\n{speaker_list}")
    else:
        await update.message.reply_text("No speakers enrolled yet. Use /enroll <Name> to add one.")


async def handle_unenroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove an enrolled speaker."""
    if not context.args:
        await update.message.reply_text("Usage: /unenroll <Name>\nExample: /unenroll Edwin")
        return

    name = " ".join(context.args)
    if remove_speaker(name):
        await update.message.reply_text(f"Removed {name} from enrolled speakers.")
    else:
        await update.message.reply_text(f"Speaker '{name}' not found.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    voice = update.message.voice
    if _is_enrolling(context):
        await _handle_enrollment(update, context, voice, ".ogg")
        return
    await _process_audio(update, context, voice, voice.file_size, ".ogg")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    audio = update.message.audio
    file_name = audio.file_name or "audio.mp3"
    ext = Path(file_name).suffix.lower()

    if _is_enrolling(context):
        await _handle_enrollment(update, context, audio, ext)
        return

    if ext not in SUPPORTED_EXTENSIONS:
        await update.message.reply_text(
            f"Unsupported format: {ext}\n"
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
        return

    await _process_audio(update, context, audio, audio.file_size, ext)


async def handle_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    video_note = update.message.video_note
    if _is_enrolling(context):
        await _handle_enrollment(update, context, video_note, ".mp4")
        return
    await _process_audio(update, context, video_note, video_note.file_size, ".mp4")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    video = update.message.video
    if _is_enrolling(context):
        await _handle_enrollment(update, context, video, ".mp4")
        return
    await _process_audio(update, context, video, video.file_size, ".mp4")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    file_name = doc.file_name or ""
    ext = Path(file_name).suffix.lower()

    if _is_enrolling(context):
        await _handle_enrollment(update, context, doc, ext)
        return

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
    app.add_handler(CommandHandler("enroll", handle_enroll))
    app.add_handler(CommandHandler("done", handle_done))
    app.add_handler(CommandHandler("speakers", handle_speakers))
    app.add_handler(CommandHandler("unenroll", handle_unenroll))
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
