import shutil
import logging
from pathlib import Path

import numpy as np
import torchaudio
from speechbrain.inference.speaker import EncoderClassifier

from config import SPEAKERS_DIR, SPEAKER_SIMILARITY_THRESHOLD

logger = logging.getLogger(__name__)

_speaker_model: EncoderClassifier | None = None


def get_speaker_model() -> EncoderClassifier:
    """Load SpeechBrain ECAPA-TDNN model on first call, reuse after."""
    global _speaker_model
    if _speaker_model is None:
        logger.info("Loading speaker embedding model (first time downloads ~200MB)...")
        _speaker_model = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            run_opts={"device": "cpu"},
        )
        logger.info("Speaker embedding model loaded.")
    return _speaker_model


def extract_embedding(audio_path: Path, start: float | None = None, end: float | None = None) -> np.ndarray | None:
    """Extract a speaker embedding from an audio file or a segment of it."""
    waveform, sample_rate = torchaudio.load(str(audio_path))

    # Convert to mono if stereo
    if waveform.shape[0] > 1:
        waveform = waveform.mean(dim=0, keepdim=True)

    # Resample to 16kHz if needed
    if sample_rate != 16000:
        resampler = torchaudio.transforms.Resample(orig_freq=sample_rate, new_freq=16000)
        waveform = resampler(waveform)
        sample_rate = 16000

    # Extract segment if start/end provided
    if start is not None or end is not None:
        start_sample = int((start or 0) * sample_rate)
        end_sample = int((end or (waveform.shape[1] / sample_rate)) * sample_rate)
        waveform = waveform[:, start_sample:end_sample]

    # Skip if too short (< 0.5 seconds)
    if waveform.shape[1] < sample_rate * 0.5:
        return None

    model = get_speaker_model()
    embedding = model.encode_batch(waveform)
    return embedding.squeeze().cpu().numpy()


def enroll_speaker(name: str, audio_path: Path) -> str:
    """Enroll a speaker from an audio clip. Returns status message."""
    speaker_dir = SPEAKERS_DIR / name
    speaker_dir.mkdir(exist_ok=True)

    # Copy the audio file to the speaker's directory
    clip_number = len(list(speaker_dir.glob("clip_*"))) + 1
    ext = audio_path.suffix
    dest = speaker_dir / f"clip_{clip_number}{ext}"
    shutil.copy2(audio_path, dest)

    # Extract embedding from this clip
    embedding = extract_embedding(audio_path)
    if embedding is None:
        dest.unlink()
        return f"Audio too short. Please send at least 0.5 seconds of speech."

    # If existing embedding exists, average with new one
    embedding_path = speaker_dir / "embedding.npy"
    if embedding_path.exists():
        existing = np.load(embedding_path)
        # Weighted average: give more weight to more clips
        embedding = (existing * (clip_number - 1) + embedding) / clip_number

    np.save(embedding_path, embedding)
    logger.info(f"Enrolled speaker '{name}' with clip {clip_number}")
    return f"Enrolled {name} with clip {clip_number}. Send more clips to improve accuracy, or /done to finish."


def load_enrolled_speakers() -> dict[str, np.ndarray]:
    """Load all enrolled speaker embeddings from disk."""
    speakers = {}
    if not SPEAKERS_DIR.exists():
        return speakers

    for speaker_dir in SPEAKERS_DIR.iterdir():
        if not speaker_dir.is_dir():
            continue
        embedding_path = speaker_dir / "embedding.npy"
        if embedding_path.exists():
            speakers[speaker_dir.name] = np.load(embedding_path)

    return speakers


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def _cluster_unknown_speakers(embeddings: list[tuple[int, np.ndarray]], threshold: float = 0.75) -> dict[int, int]:
    """Cluster unknown speaker embeddings and assign consistent Speaker N labels.
    Returns mapping of segment_index -> cluster_id."""
    if not embeddings:
        return {}

    clusters: list[list[int]] = []  # each cluster is a list of segment indices
    cluster_centroids: list[np.ndarray] = []

    for seg_idx, emb in embeddings:
        best_cluster = -1
        best_sim = -1.0
        for c_idx, centroid in enumerate(cluster_centroids):
            sim = _cosine_similarity(emb, centroid)
            if sim > best_sim:
                best_sim = sim
                best_cluster = c_idx

        if best_cluster >= 0 and best_sim >= threshold:
            clusters[best_cluster].append(seg_idx)
            # Update centroid as running average
            n = len(clusters[best_cluster])
            cluster_centroids[best_cluster] = (cluster_centroids[best_cluster] * (n - 1) + emb) / n
        else:
            clusters.append([seg_idx])
            cluster_centroids.append(emb.copy())

    result = {}
    for cluster_id, seg_indices in enumerate(clusters):
        for seg_idx in seg_indices:
            result[seg_idx] = cluster_id + 1  # 1-based numbering
    return result


def identify_speakers(audio_path: Path, segments: list, enrolled: dict[str, np.ndarray]) -> list:
    """Identify speakers for each segment. Modifies segment.speaker in place and returns segments."""
    if not enrolled or not segments:
        return segments

    unknown_embeddings = []  # (segment_index, embedding) for clustering

    for i, seg in enumerate(segments):
        # For long segments, use just the first 10 seconds for embedding
        end_time = min(seg.end, seg.start + 10.0) if (seg.end - seg.start) > 10.0 else seg.end

        embedding = extract_embedding(audio_path, start=seg.start, end=end_time)
        if embedding is None:
            # Too short to identify — leave speaker as None
            continue

        # Match against enrolled speakers
        best_name = None
        best_sim = -1.0
        for name, ref_embedding in enrolled.items():
            sim = _cosine_similarity(embedding, ref_embedding)
            if sim > best_sim:
                best_sim = sim
                best_name = name

        if best_name and best_sim >= SPEAKER_SIMILARITY_THRESHOLD:
            seg.speaker = best_name
        else:
            unknown_embeddings.append((i, embedding))

    # Cluster unknown speakers for consistent labeling
    if unknown_embeddings:
        cluster_map = _cluster_unknown_speakers(unknown_embeddings)
        for seg_idx, cluster_id in cluster_map.items():
            segments[seg_idx].speaker = f"Speaker {cluster_id}"

    return segments


def list_enrolled_speakers() -> list[str]:
    """Return list of enrolled speaker names."""
    if not SPEAKERS_DIR.exists():
        return []
    return sorted([
        d.name for d in SPEAKERS_DIR.iterdir()
        if d.is_dir() and (d / "embedding.npy").exists()
    ])


def remove_speaker(name: str) -> bool:
    """Remove an enrolled speaker. Returns True if found and removed."""
    speaker_dir = SPEAKERS_DIR / name
    if speaker_dir.exists() and speaker_dir.is_dir():
        shutil.rmtree(speaker_dir)
        logger.info(f"Removed speaker '{name}'")
        return True
    return False
