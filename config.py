from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TMP_DIR = Path("./tmp")
TMP_DIR.mkdir(exist_ok=True)

WHISPER_MODEL = "large-v3"
WHISPER_COMPUTE_TYPE = "int8"
MAX_AUDIO_SIZE_MB = 20
SUPPORTED_EXTENSIONS = {".ogg", ".mp3", ".wav", ".m4a"}
