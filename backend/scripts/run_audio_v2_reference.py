from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

import config  # noqa: E402
from AI.audio_pipeline_v2 import AudioPipelineV2, AudioPipelineV2Request  # noqa: E402
from AI.reference_quality import compare_lyrics_documents  # noqa: E402
from AI.runtime import configure_runtime  # noqa: E402


def _progress(stage: str, percent: float, detail: str) -> None:
    print(f"[v2] {percent:5.1f}% {stage}: {detail}", flush=True)


def _reference_root() -> Path:
    return ROOT / "generated" / "diagnostics" / "audio-reference-corpus"


def run_one(
    pipeline: AudioPipelineV2,
    reference_dir: Path,
    *,
    processing_mode: str = "fast",
) -> dict:
    reference = json.loads(
        (reference_dir / "lyricsSync.json").read_text(encoding="utf-8")
    )
    artist = str(reference["artist"])
    title = str(reference["title"])
    source = ROOT / "karaoke_songs" / f"{reference_dir.name}.flac"
    output = (
        ROOT / "generated" / "diagnostics" / "audio-v2-pipeline" / reference_dir.name
    )
    output.mkdir(parents=True, exist_ok=True)
    pipeline.run(
        AudioPipelineV2Request(
            source,
            output,
            artist=artist,
            title=title,
            language="Russian",
            processing_mode=processing_mode,
            progress=_progress,
        )
    )
    candidate = json.loads(
        (output / "lyricsSync.json").read_text(encoding="utf-8")
    )
    quality = compare_lyrics_documents(reference, candidate)
    result = {
        "song": reference_dir.name,
        "reference_words": len(reference["words"]),
        "candidate_words": len(candidate["words"]),
        "token_similarity": quality.token_similarity,
        "matched_word_ratio": quality.matched_word_ratio,
        "onset_mae_seconds": quality.onset_mae_seconds,
        "onset_p95_seconds": quality.onset_p95_seconds,
        "pitch_match_ratio": quality.pitch_match_ratio,
        "note_duration_mae_seconds": quality.note_duration_mae_seconds,
        "note_duration_ratio": quality.note_duration_ratio,
        "note_count_ratio": quality.note_count_ratio,
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--song", help="Reference directory name; omit for all songs")
    parser.add_argument(
        "--mode",
        choices=("fast", "quality"),
        default="fast",
        help="Audio-v2 processing mode used for the comparison",
    )
    parser.add_argument(
        "--neural-transcript",
        action="store_true",
        help="Print the neural ASR transcript for one already separated song",
    )
    arguments = parser.parse_args()
    config.configure_ai_resource_environment(force=True)
    configure_runtime("auto", force=True)
    references = sorted(
        (path for path in _reference_root().iterdir() if path.is_dir()),
        key=lambda path: path.name,
    )
    if arguments.song:
        references = [path for path in references if path.name == arguments.song]
        if not references:
            raise SystemExit(f"Unknown reference song: {arguments.song}")
    pipeline = AudioPipelineV2()
    results = []
    try:
        if arguments.neural_transcript:
            if len(references) != 1:
                raise SystemExit("--neural-transcript requires exactly one --song")
            vocals = (
                ROOT / "generated" / "diagnostics" / "audio-v2-pipeline"
                / references[0].name / "vocals.flac"
            )
            text, _words = pipeline.engines.transcriber.transcribe_neural(
                vocals, "Russian"
            )
            print(text, flush=True)
            return 0
        for reference in references:
            try:
                results.append(
                    run_one(
                        pipeline,
                        reference,
                        processing_mode=arguments.mode,
                    )
                )
            except Exception as exc:
                failure = {
                    "song": reference.name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                results.append(failure)
                print(json.dumps(failure, ensure_ascii=False), flush=True)
    finally:
        pipeline.close()
    report = ROOT / "generated" / "diagnostics" / "audio-v2-reference-report.json"
    report.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
