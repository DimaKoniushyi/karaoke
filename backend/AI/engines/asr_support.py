from __future__ import annotations

import unicodedata


def context_echo_key(value: str) -> str:
    return "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", str(value))
        if character.isalnum()
    )


def prepare_greedy_generation(wrapper) -> None:
    """Make deterministic Qwen generation explicit for current Transformers."""
    if wrapper is None or getattr(wrapper, "backend", "transformers") != "transformers":
        return
    model = getattr(wrapper, "model", None)
    root_generation = getattr(model, "generation_config", None)
    root_config = getattr(model, "config", None)
    fallback_eos = getattr(root_generation, "eos_token_id", None)
    if fallback_eos is None:
        fallback_eos = getattr(root_config, "eos_token_id", None)
    if isinstance(fallback_eos, (list, tuple)):
        fallback_eos = fallback_eos[0] if fallback_eos else None

    for target in (model, getattr(model, "thinker", None)):
        generation = getattr(target, "generation_config", None)
        if generation is None:
            continue
        if not bool(getattr(generation, "do_sample", False)):
            for name in ("temperature", "top_p", "top_k"):
                if hasattr(generation, name):
                    setattr(generation, name, None)
        if getattr(generation, "pad_token_id", None) is not None:
            continue
        target_config = getattr(target, "config", None)
        eos = getattr(generation, "eos_token_id", None)
        if eos is None:
            eos = getattr(target_config, "eos_token_id", None)
        if isinstance(eos, (list, tuple)):
            eos = eos[0] if eos else None
        eos = eos if eos is not None else fallback_eos
        if eos is not None:
            generation.pad_token_id = eos
            if (
                target_config is not None
                and getattr(target_config, "pad_token_id", None) is None
            ):
                target_config.pad_token_id = eos


class TimedAudioChunks(list):
    def __init__(self, chunks, windows, line_starts=None):
        super().__init__(chunks)
        self.windows = tuple(windows)
        self.line_starts = tuple(
            line_starts
            if line_starts is not None
            else (start for start, _end in self.windows)
        )


def asr_voice_chunks(audio, max_seconds: float = 30.0):
    """Batch long vocals while retaining complete audio and local voice anchors."""
    import numpy as np

    from ..audio import read_mono
    from ..word_voicing import voice_activity_intervals

    samples, rate = read_mono(audio)
    intervals = voice_activity_intervals(audio)
    chunk_frames = max(1, round(max_seconds * rate))
    if len(samples) <= chunk_frames:
        return TimedAudioChunks(
            [(samples.astype(np.float32), rate)],
            [(0.0, len(samples) / rate)],
            [intervals[0][0] if intervals else 0.0],
        )
    chunks = []
    windows = []
    for lower in range(0, len(samples), chunk_frames):
        upper = min(len(samples), lower + chunk_frames)
        chunks.append((samples[lower:upper].astype(np.float32), rate))
        windows.append((lower / rate, upper / rate))
    line_starts = [
        next(
            (start for start, _end in intervals if lower <= start < upper),
            lower,
        )
        for lower, upper in windows
    ]
    return TimedAudioChunks(chunks, windows, line_starts)
