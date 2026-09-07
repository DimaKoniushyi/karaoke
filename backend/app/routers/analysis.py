"""Voice-analysis endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError

import models
import schemas
from app import repositories
from app.api.dependencies import AnalysisDependency, DatabaseSession, RecordingDependency
from app.api.errors import http_error
from app.services import analysis_service
from app.services.db_utils import commit_refresh
from app.utils.json_values import parse_json_value

router = APIRouter(prefix="/analysis", tags=["analysis"])


def _to_out(result: models.AnalysisResult) -> schemas.AnalysisOut:
    return schemas.AnalysisOut(
        id=result.id,
        recording_id=result.recording_id,
        pitch_accuracy_percent=result.pitch_accuracy_percent,
        mean_deviation_semitones=result.mean_deviation_semitones,
        rhythm_accuracy_percent=result.rhythm_accuracy_percent,
        note_hold_percent=result.note_hold_percent,
        note_coverage_percent=result.note_coverage_percent,
        overall_score_percent=result.overall_score_percent,
        sections=parse_json_value(result.sections_json, None),
        created_at=result.created_at,
    )


def _apply_analysis(result: models.AnalysisResult, analysis: dict) -> None:
    for name in (
        "pitch_accuracy_percent",
        "mean_deviation_semitones",
        "rhythm_accuracy_percent",
        "note_hold_percent",
        "note_coverage_percent",
        "overall_score_percent",
    ):
        setattr(result, name, analysis.get(name))
    result.sections_json = (
        json.dumps(analysis["sections"], ensure_ascii=False) if analysis.get("sections") else None
    )


@router.post("/{recording_id}/run", response_model=schemas.AnalysisOut)
def run_analysis(recording: RecordingDependency, db: DatabaseSession):
    song = repositories.get_song(db, recording.song_id)
    if song is None: raise HTTPException(status_code=404, detail="Песня для этой записи не найдена")

    existing = repositories.get_analysis_by_recording(db, recording.id)
    if existing is not None and existing.overall_score_percent is not None: return _to_out(existing)

    with http_error(ValueError, 409): analysis = analysis_service.analyze_recording(recording, song)

    if existing is not None:
        _apply_analysis(existing, analysis)
        return _to_out(commit_refresh(db, existing))

    result = models.AnalysisResult(recording_id=recording.id)
    _apply_analysis(result, analysis)
    db.add(result)
    try:
        result = commit_refresh(db, result)
    except IntegrityError:
        stored_result = repositories.get_analysis_by_recording(db, recording.id)
        if stored_result is None: raise
        result = stored_result
    return _to_out(result)


@router.get("/{recording_id}", response_model=schemas.AnalysisOut)
def get_analysis(result: AnalysisDependency):
    return _to_out(result)


@router.get("/{recording_id}/accuracy")
def get_accuracy(result: AnalysisDependency):
    return {"pitch_accuracy_percent": result.pitch_accuracy_percent}


@router.get("/{recording_id}/deviation")
def get_deviation(result: AnalysisDependency):
    return {"mean_deviation_semitones": result.mean_deviation_semitones}


@router.get("/{recording_id}/sections")
def get_sections(result: AnalysisDependency):
    return {"sections": parse_json_value(result.sections_json, None)}
