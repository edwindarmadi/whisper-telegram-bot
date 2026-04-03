from dataclasses import dataclass
from pathlib import Path
from faster_whisper import WhisperModel
from config import WHISPER_MODEL, WHISPER_COMPUTE_TYPE

_model: WhisperModel | None = None


@dataclass
class TranscriptionResult:
    text: str
    language: str
    duration: float


def get_model() -> WhisperModel:
    """Load model on first call, reuse after. First call downloads ~3GB."""
    global _model
    if _model is None:
        print(f"Loading Whisper model '{WHISPER_MODEL}' (first time downloads ~3GB)...")
        _model = WhisperModel(WHISPER_MODEL, device="auto", compute_type=WHISPER_COMPUTE_TYPE)
        print("Model loaded.")
    return _model


def transcribe_audio(file_path: Path) -> TranscriptionResult:
    """Synchronous — call via asyncio.to_thread() from async code."""
    model = get_model()
    segments, info = model.transcribe(str(file_path), beam_size=5, vad_filter=True)

    paragraphs = [segment.text.strip() for segment in segments]

    return TranscriptionResult(
        text="\n\n".join(paragraphs),
        language=info.language,
        duration=info.duration,
    )
