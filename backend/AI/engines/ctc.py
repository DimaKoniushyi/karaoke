from __future__ import annotations

import math
import warnings

import numpy as np

from ..errors import EngineUnavailableError, InvalidArtifactError
from ..models import Word
from .device import select_torch_device


def _ctc_audio_windows(
    sample_count: int,
    sample_rate: int,
    *,
    max_seconds: float = 45.0,
    overlap_seconds: float = 1.0,
) -> list[tuple[int, int]]:
    """Cover long audio with bounded, slightly overlapping inference windows."""
    count = max(0, int(sample_count))
    rate = max(1, int(sample_rate))
    maximum = max(1, round(rate * max_seconds))
    overlap = min(maximum - 1, max(0, round(rate * overlap_seconds)))
    if count <= maximum:
        return [(0, count)]
    windows, start = [], 0
    while start < count:
        end = min(count, start + maximum)
        windows.append((start, end))
        if end >= count:
            break
        start = end - overlap
    return windows


class CTCWordAligner:
    def __init__(self, model_path, role="ctc"):
        self.model_path = str(model_path)
        self.role = str(role)
        self._model = self._processor = self._device = None

    def _load(self):
        import torch

        if self._model is None:
            from transformers import AutoModelForCTC, Wav2Vec2Processor

            self._device = select_torch_device(torch, self.role)
            self._processor = Wav2Vec2Processor.from_pretrained(self.model_path)
            self._model = AutoModelForCTC.from_pretrained(self.model_path).to(self._device).eval()
        else:
            device = select_torch_device(torch, self.role)
            if str(next(self._model.parameters()).device) != device:
                self._model.to(device)
            self._device = device
        return self._model, self._processor

    def _infer_logits(self, audio, target_rate: int, model, processor):
        import torch
        inputs = processor(audio.numpy(), sampling_rate=target_rate, return_tensors="pt")
        with torch.inference_mode():
            return model(inputs.input_values.to(self._device)).logits.log_softmax(dim=-1)

    def _compute_logits(self, samples, rate: int):
        import torch

        model, processor = self._load()
        audio = torch.as_tensor(np.asarray(samples), dtype=torch.float32)
        if audio.ndim > 1:
            audio = audio.mean(dim=1)
        target_rate = int(processor.feature_extractor.sampling_rate)
        if rate != target_rate:
            import torchaudio

            audio = torchaudio.functional.resample(audio, rate, target_rate)
        windows = _ctc_audio_windows(len(audio), target_rate)
        window_logits = [
            self._infer_logits(audio[start:end], target_rate, model, processor)
            for start, end in windows
        ]
        if len(window_logits) == 1:
            logits = window_logits[0]
        else:
            trimmed = []
            for index, ((start, end), values) in enumerate(
                zip(windows, window_logits, strict=True)
            ):
                frames = int(values.shape[1])
                left_boundary = (
                    start
                    if index == 0
                    else (start + windows[index - 1][1]) / 2
                )
                right_boundary = (
                    end
                    if index + 1 == len(windows)
                    else (end + windows[index + 1][0]) / 2
                )
                scale = frames / max(1, end - start)
                lower = max(0, min(frames, round((left_boundary - start) * scale)))
                upper = max(lower + 1, min(frames, round((right_boundary - start) * scale)))
                trimmed.append(values[:, lower:upper, :])
            logits = torch.cat(trimmed, dim=1)
        duration = len(audio) / target_rate
        return logits, model, processor, duration

    def _acoustic_logits(self, audio):
        from ..audio import read_mono

        samples, rate = read_mono(audio)
        return self._compute_logits(samples, rate)

    @staticmethod
    def _align_logits(logits, model, processor, duration, tokens, offset=0.0):
        import torch
        import torchaudio

        delimiter = processor.tokenizer.word_delimiter_token_id
        targets, ranges = [], []
        for token in tokens:
            if targets:
                targets.append(delimiter)
            start = len(targets)
            ids = processor.tokenizer.encode(token.casefold(), add_special_tokens=False)
            ids = [value for value in ids if value != processor.tokenizer.unk_token_id]
            if not ids:
                raise InvalidArtifactError(f"CTC vocabulary cannot encode {token!r}")
            targets.extend(ids)
            ranges.append((start, len(targets)))
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*forced_align has been deprecated.*")
                path, scores = torchaudio.functional.forced_align(
                    logits, torch.tensor([targets], device=logits.device),
                    blank=int(model.config.pad_token_id),
                )
        except (RuntimeError, ValueError) as error:
            raise EngineUnavailableError(f"CTC forced alignment failed: {error}") from error
        labels, values = path[0].tolist(), scores[0].tolist()
        spans, cursor = [], 0
        for target in targets:
            try:
                start = labels.index(target, cursor)
            except ValueError as error:
                raise InvalidArtifactError("CTC path does not contain the canonical transcript") from error
            end = start + 1
            while end < len(labels) and labels[end] == target:
                end += 1
            spans.append((start, end, values[start:end]))
            cursor = end
        seconds = duration / len(labels)
        words = [
            Word(
                offset + spans[start][0] * seconds,
                offset + spans[end - 1][1] * seconds,
                token,
                min(1.0, math.exp(sum(sum(item[2]) for item in spans[start:end]) / sum(len(item[2]) for item in spans[start:end]))),
                index,
            )
            for index, (token, (start, end)) in enumerate(zip(tokens, ranges, strict=True))
        ]
        frame_count = sum(len(span[2]) for span in spans)
        score = sum(sum(span[2]) for span in spans) / max(1, frame_count)
        return score, words

    def align(self, samples, rate: int, tokens: list[str], offset: float) -> list[Word]:
        logits, model, processor, duration = self._compute_logits(samples, rate)
        _score, words = self._align_logits(
            logits, model, processor, duration, tokens, offset
        )
        return words

    def _align_split_transcripts(
        self,
        logits,
        model,
        processor,
        duration,
        transcripts,
        split_seconds,
    ):
        if not transcripts or not 0 < split_seconds < duration:
            return None
        common = 0
        for values in zip(*transcripts, strict=False):
            if len(set(values)) != 1:
                break
            common += 1
        if common == 0:
            return None
        frames = int(logits.shape[1])
        split_frame = min(
            frames - 1,
            max(1, round(frames * split_seconds / duration)),
        )
        prefix_score, prefix_words = self._align_logits(
            logits[:, :split_frame, :],
            model,
            processor,
            split_seconds,
            transcripts[0][:common],
            0.0,
        )
        results = []
        for tokens in transcripts:
            tail = tokens[common:]
            try:
                tail_score, tail_words = self._align_logits(
                    logits[:, split_frame:, :],
                    model,
                    processor,
                    duration - split_seconds,
                    tail,
                    split_seconds,
                )
            except (EngineUnavailableError, InvalidArtifactError):
                results.append(None)
                continue
            total = common + len(tail)
            score = (
                prefix_score * common + tail_score * len(tail)
            ) / max(1, total)
            words = [
                Word(word.start, word.end, word.text, word.confidence, index)
                for index, word in enumerate(prefix_words + tail_words)
            ]
            results.append((score, words))
        return results

    def align_transcripts(self, audio, transcripts, *, split_seconds=None):
        """Align alternatives while sharing one expensive model inference."""
        logits, model, processor, duration = self._acoustic_logits(audio)
        if split_seconds is not None:
            split = self._align_split_transcripts(
                logits,
                model,
                processor,
                duration,
                transcripts,
                split_seconds,
            )
            if split is not None:
                return split
        results = []
        for tokens in transcripts:
            try:
                results.append(self._align_logits(
                    logits, model, processor, duration, tokens, 0.0
                ))
            except (EngineUnavailableError, InvalidArtifactError):
                results.append(None)
        return results

    def transcribe(self, audio, *, max_window_seconds: float = 22.0) -> list[Word]:
        """Greedy CTC transcription with native word timestamps.

        Short voice-aware windows avoid the quadratic memory/time cost of a
        whole-song wav2vec pass.  Unlike generative ASR, decoding cannot run
        away to hundreds of output tokens when a vocal stem is noisy.
        """
        import torch
        import torchaudio

        from ..audio import read_mono
        from ..word_voicing import voice_activity_intervals

        samples, rate = read_mono(audio)
        intervals = voice_activity_intervals(audio)
        span = len(samples) / max(1, rate)
        if not intervals:
            intervals = [(0.0, span)]

        windows: list[tuple[float, float]] = []
        first, previous_end = intervals[0]
        for start, end in intervals[1:]:
            if end - first > max_window_seconds:
                windows.append((first, previous_end))
                first = start
            previous_end = end
        windows.append((first, previous_end))

        bounded: list[tuple[float, float]] = []
        for start, end in windows:
            start, end = max(0.0, start - 0.08), min(span, end + 0.08)
            while end - start > max_window_seconds:
                bounded.append((start, start + max_window_seconds))
                start += max_window_seconds
            if end - start >= 0.5:
                bounded.append((start, end))

        model, processor = self._load()
        target_rate = int(processor.feature_extractor.sampling_rate)
        words: list[Word] = []
        for window_start, window_end in bounded:
            lower, upper = round(window_start * rate), round(window_end * rate)
            signal = torch.as_tensor(samples[lower:upper], dtype=torch.float32)
            if rate != target_rate:
                signal = torchaudio.functional.resample(signal, rate, target_rate)
            inputs = processor(
                signal.numpy(), sampling_rate=target_rate, return_tensors="pt"
            )
            with torch.inference_mode():
                logits = model(inputs.input_values.to(self._device)).logits[0]
            predicted = torch.argmax(logits, dim=-1)
            decoded = processor.tokenizer.decode(
                predicted, output_word_offsets=True
            )
            time_offset = (
                float(model.config.inputs_to_logits_ratio) / target_rate
            )
            for item in decoded.word_offsets or ():
                token = str(item.get("word") or "").strip().casefold()
                if not token:
                    continue
                start = window_start + float(item["start_offset"]) * time_offset
                end = window_start + float(item["end_offset"]) * time_offset
                if end > start:
                    words.append(Word(start, end, token, 0.7, len(words)))
        return words

    def close(self) -> None:
        self._model = self._processor = self._device = None

    def park(self) -> None:
        if self._model is not None:
            self._model.to("cpu")
            self._device = "cpu"
