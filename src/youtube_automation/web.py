from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, UTC
from pathlib import Path
from threading import Lock
from uuid import uuid4
import html
import json
import re
import statistics
import subprocess

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .characters import CHARACTER_PROFILES, normalize_character_profile
from .config import Settings, load_dotenv
from .ffmpeg import render_video_from_clips_with_clip_audio
from .kie import KieClient, find_urls
from .pipeline import VideoPipeline
from .planner import plan_from_script
from .reference_video import validate_youtube_url
from .script_generator import ScriptGenerator
from .styles import normalize_style


@dataclass(slots=True)
class JobRecord:
    job_id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    scene_count: int = 0
    error: str | None = None
    output_url: str | None = None


@dataclass(slots=True)
class JobInput:
    script_text: str
    reference_url: str
    video_style: str = "cinematic"
    character: str = "auto"


class ScriptRequest(BaseModel):
    topic: str
    angle: str = ""
    tone: str = "engaging"
    target_words: int = 150
    video_style: str = "cinematic"
    character: str = "auto"


class VoiceTestRequest(BaseModel):
    text: str = "Hello, this is a voice test for the current ElevenLabs voice configuration."


load_dotenv()
app = FastAPI(title="YouTube Automation")
BASE_BUILD_DIR = Path("build/web/jobs")
BASE_BUILD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/outputs", StaticFiles(directory="build"), name="outputs")

_jobs: dict[str, JobRecord] = {}
_job_inputs: dict[str, JobInput] = {}
_jobs_lock = Lock()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _save_job(job: JobRecord) -> None:
    with _jobs_lock:
        _jobs[job.job_id] = job
    _write_job_record(job)


def _save_job_input(job_id: str, job_input: JobInput) -> None:
    with _jobs_lock:
        _job_inputs[job_id] = job_input
    _write_job_input(job_id, job_input)


def _get_job(job_id: str) -> JobRecord:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        job = _read_job_record(job_id)
        if job:
            with _jobs_lock:
                _jobs[job_id] = job
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


def _get_job_input(job_id: str) -> JobInput:
    with _jobs_lock:
        job_input = _job_inputs.get(job_id)
    if not job_input:
        job_input = _read_job_input(job_id)
        if job_input:
            with _jobs_lock:
                _job_inputs[job_id] = job_input
    if not job_input:
        raise HTTPException(status_code=404, detail="Job input not found.")
    return job_input


def _update_job(job_id: str, **updates: str | int | None) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        for key, value in updates.items():
            setattr(job, key, value)
        job.updated_at = _utc_now()
    _write_job_record(job)


def _job_output_url(video_path: Path) -> str:
    relative = video_path.relative_to(Path("build"))
    return f"/outputs/{relative.as_posix()}"


def _cache_busted_output_url(video_path: Path) -> str:
    return f"{_job_output_url(video_path)}?v={int(video_path.stat().st_mtime)}"


def _path_from_output_url(output_url: str | None) -> Path | None:
    if not output_url or not output_url.startswith("/outputs/"):
        return None
    relative_url = output_url.split("?", 1)[0].removeprefix("/outputs/")
    return Path("build") / relative_url


def _voice_test_dir() -> Path:
    path = Path("build/web/voice-tests")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_dir(job_id: str) -> Path:
    return BASE_BUILD_DIR / job_id


def _job_record_path(job_id: str) -> Path:
    return _job_dir(job_id) / "job.json"


def _job_input_path(job_id: str) -> Path:
    return _job_dir(job_id) / "job_input.json"


def _write_job_record(job: JobRecord) -> None:
    path = _job_record_path(job.job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(job), indent=2), encoding="utf-8")


def _write_job_input(job_id: str, job_input: JobInput) -> None:
    path = _job_input_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(job_input), indent=2), encoding="utf-8")


def _read_job_record(job_id: str) -> JobRecord | None:
    path = _job_record_path(job_id)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return JobRecord(**payload)


def _read_job_input(job_id: str) -> JobInput | None:
    path = _job_input_path(job_id)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("video_style", "cinematic")
    payload.pop("veo_enable_fallback", None)
    return JobInput(**payload)


def _parse_iso_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_eta(seconds: float | None) -> str | None:
    if seconds is None or seconds <= 0:
        return None
    rounded = int(round(seconds))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m left"
    if minutes:
        return f"{minutes}m {secs:02d}s left"
    return f"{secs}s left"


def _job_clip_count(job_id: str) -> int:
    return len(_scene_clip_paths(job_id))


def _scene_clip_paths(job_id: str) -> list[Path]:
    assets_dir = _job_dir(job_id) / "assets"
    if not assets_dir.exists():
        return []
    return sorted(path for path in assets_dir.glob("scene_*.mp4") if re.fullmatch(r"scene_\d+\.mp4", path.name))


def _preview_clip_path(scene_path: Path) -> Path:
    return scene_path.with_name(f"{scene_path.stem}.preview.mp4")


def _ensure_streamable_preview(video_path: Path) -> Path:
    preview_path = _preview_clip_path(video_path)
    if preview_path.exists() and preview_path.stat().st_mtime >= video_path.stat().st_mtime and preview_path.stat().st_size > 0:
        return preview_path

    preview_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(preview_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Could not prepare streamable preview for {video_path.name}: {completed.stderr.strip()}")
    return preview_path


def _streamable_preview_url(video_path: Path | None) -> str | None:
    if not video_path or not video_path.exists() or video_path.stat().st_size <= 0:
        return None
    preview_path = _ensure_streamable_preview(video_path)
    return _cache_busted_output_url(preview_path)


def _preview_endpoint_url(job_id: str, kind: str, source_path: Path | None) -> str | None:
    if not source_path or not source_path.exists() or source_path.stat().st_size <= 0:
        return None
    return f"/api/jobs/{job_id}/preview/{kind}?v={int(source_path.stat().st_mtime)}"


def _job_scene_clip_urls(job_id: str) -> list[dict[str, object]]:
    assets_dir = _job_dir(job_id) / "assets"
    clips: list[dict[str, object]] = []
    if not assets_dir.exists():
        return clips

    for path in sorted(assets_dir.glob("scene_*.mp4")):
        if not path.exists() or path.stat().st_size <= 0:
            continue
        if not re.fullmatch(r"scene_\d+\.mp4", path.name):
            continue
        match = re.search(r"scene_(\d+)\.mp4$", path.name)
        index = int(match.group(1)) if match else len(clips) + 1
        preview_url = _streamable_preview_url(path)
        if preview_url:
            clips.append({"index": index, "url": preview_url})
    return clips


def _historical_scene_seconds(current_job_id: str) -> float | None:
    samples: list[float] = []
    for path in BASE_BUILD_DIR.glob("*/job.json"):
        job_id = path.parent.name
        if job_id == current_job_id:
            continue
        job = _read_job_record(job_id)
        if not job:
            continue

        clip_count = _job_clip_count(job_id)
        if clip_count <= 0:
            continue

        created_at = _parse_iso_datetime(job.created_at)
        updated_at = _parse_iso_datetime(job.updated_at)
        if not created_at or not updated_at:
            continue

        elapsed_seconds = max((updated_at - created_at).total_seconds(), 1.0)
        scene_seconds = elapsed_seconds / clip_count
        if 5 <= scene_seconds <= 3600:
            samples.append(scene_seconds)

    if not samples:
        return None
    return float(statistics.median(samples))


def _job_progress(job: JobRecord) -> dict[str, object]:
    job_dir = _job_dir(job.job_id)
    assets_dir = job_dir / "assets"
    clip_count = _job_clip_count(job.job_id)
    narration_exists = (assets_dir / "narration.mp3").exists()

    total = max(job.scene_count, 1)
    current = min(clip_count, total)
    percent = int((current / total) * 100)
    remaining = max(total - current, 0)

    stage = "Queued"
    if narration_exists and clip_count == 0:
        stage = "Narration ready"
    elif clip_count > 0:
        stage = f"Scenes rendered: {clip_count}/{total}"

    eta_seconds: float | None = None
    if job.status in {"pending", "rendering"} and remaining > 0:
        historical_scene_seconds = _historical_scene_seconds(job.job_id)
        current_pace_seconds: float | None = None
        created_at = _parse_iso_datetime(job.created_at)
        if created_at and current > 0:
            current_pace_seconds = max((datetime.now(UTC) - created_at).total_seconds() / current, 1.0)

        if historical_scene_seconds is not None and current_pace_seconds is not None:
            eta_seconds = remaining * ((historical_scene_seconds * 0.65) + (current_pace_seconds * 0.35))
        elif current_pace_seconds is not None:
            eta_seconds = remaining * current_pace_seconds
        elif historical_scene_seconds is not None:
            eta_seconds = remaining * historical_scene_seconds

    if job.status == "completed":
        percent = 100
        current = total
        stage = "Completed"
        eta_seconds = 0

    eta_label = _format_eta(eta_seconds)
    if eta_label and job.status in {"pending", "rendering"}:
        stage = f"{stage} • ETA {eta_label}"

    return {
        "progress_current": current,
        "progress_total": total,
        "progress_percent": percent,
        "progress_stage": stage,
        "progress_eta_seconds": int(round(eta_seconds)) if eta_seconds else None,
        "progress_eta_label": eta_label,
    }


def _clip_audio_output_path(job_id: str, title: str) -> Path:
    slug = title.strip().lower()
    slug = "".join(ch if ch.isalnum() else "-" for ch in slug)
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-") or "video"
    return _job_dir(job_id) / f"{slug}-clip-audio.mp4"


def _clip_audio_status_path(job_id: str) -> Path:
    return _job_dir(job_id) / "clip_audio_status.json"


def _write_clip_audio_status(job_id: str, status: str, error: str | None = None) -> None:
    path = _clip_audio_status_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": status,
                "error": error,
                "updated_at": _utc_now(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _read_clip_audio_status(job_id: str) -> dict[str, str | None]:
    path = _clip_audio_status_path(job_id)
    if not path.exists():
        return {"status": "idle", "error": None, "updated_at": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "idle", "error": None, "updated_at": None}
    return {
        "status": str(payload.get("status") or "idle"),
        "error": payload.get("error"),
        "updated_at": payload.get("updated_at"),
    }


def _clip_audio_status_is_fresh(status: dict[str, str | None]) -> bool:
    updated_at = status.get("updated_at")
    if not updated_at:
        return False
    parsed = _parse_iso_datetime(updated_at)
    if not parsed:
        return False
    return (datetime.now(UTC) - parsed).total_seconds() < 1800


def _job_clip_audio_url(job: JobRecord) -> str | None:
    path = _clip_audio_output_path(job.job_id, job.title)
    if not path.exists():
        return None
    return _job_output_url(path)


def _task_metadata_paths() -> list[Path]:
    return list(BASE_BUILD_DIR.glob("*/assets/*.task.json"))


def _find_task_metadata(task_id: str) -> tuple[str, Path] | None:
    for path in _task_metadata_paths():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if payload.get("task_id") == task_id:
            return path.parents[1].name, path
    return None


def _append_callback_log(job_id: str, payload: dict) -> None:
    log_path = _job_dir(job_id) / "callbacks.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def _update_task_metadata_from_callback(job_id: str, metadata_path: Path, payload: dict) -> None:
    task_payload: dict[str, object] = {}
    if metadata_path.exists():
        try:
            task_payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            task_payload = {}

    callback_code = payload.get("code")
    callback_msg = payload.get("msg")
    callback_data = payload.get("data") or {}
    info = callback_data.get("info") or {}
    result_urls = info.get("resultUrls")

    task_payload["callback_received_at"] = _utc_now()
    task_payload["callback_code"] = callback_code
    task_payload["callback_msg"] = callback_msg
    if result_urls:
        task_payload["callback_result_urls"] = result_urls
        task_payload["status"] = "callback_success"
    else:
        task_payload["status"] = "callback_failed"
    metadata_path.write_text(json.dumps(task_payload, indent=2), encoding="utf-8")

    if callback_code not in {200, "200"} and callback_msg:
        _update_job(job_id, error=f"Callback: {callback_msg}")


def _serialize_job(job: JobRecord) -> dict:
    payload = asdict(job)
    payload.update(_job_progress(job))
    payload["scene_clip_urls"] = _job_scene_clip_urls(job.job_id)
    payload["output_preview_url"] = _preview_endpoint_url(job.job_id, "rendered", _path_from_output_url(job.output_url))
    payload["clip_audio_output_url"] = _job_clip_audio_url(job)
    payload["clip_audio_preview_url"] = _preview_endpoint_url(
        job.job_id,
        "clip-audio",
        _clip_audio_output_path(job.job_id, job.title),
    )
    payload["clip_audio_status"] = _read_clip_audio_status(job.job_id)
    return payload


def _run_job(
    job_id: str,
    script_text: str,
    reference_url: str,
    output_dir: Path,
    video_style: str = "cinematic",
    character: str = "auto",
) -> None:
    try:
        settings = Settings.from_env()
        plan = plan_from_script(script_text, video_style=video_style, character=character)
        _update_job(job_id, status="rendering", title=plan.title, scene_count=len(plan.scenes))

        pipeline = VideoPipeline(settings)
        video_path = pipeline.create_video(plan, reference_url, output_dir)
        _update_job(job_id, status="completed", output_url=_job_output_url(video_path))
    except Exception as exc:
        _update_job(job_id, status="failed", error=str(exc))


def _retry_job(background_tasks: BackgroundTasks, job_id: str) -> JobRecord:
    job = _get_job(job_id)
    job_input = _get_job_input(job_id)

    if job.status not in {"failed", "completed"}:
        raise HTTPException(status_code=400, detail="Only failed or completed jobs can be retried.")

    output_dir = BASE_BUILD_DIR / job_id
    _update_job(job_id, status="pending", error=None, output_url=None)
    background_tasks.add_task(
        _run_job,
        job_id,
        job_input.script_text,
        job_input.reference_url,
        output_dir,
        job_input.video_style,
        job_input.character,
    )
    return _get_job(job_id)


def _cancel_job(job_id: str) -> JobRecord:
    job = _get_job(job_id)
    if job.status not in {"pending", "rendering"}:
        raise HTTPException(status_code=400, detail="Only pending or rendering jobs can be cancelled.")

    _update_job(
        job_id,
        status="failed",
        error="Cancelled locally so you can retry.",
        output_url=None,
    )
    return _get_job(job_id)


def _scene_task_path(job_id: str, scene_index: int) -> Path:
    return _job_dir(job_id) / "assets" / f"scene_{scene_index:03d}.task.json"


def _scene_clip_path(job_id: str, scene_index: int) -> Path:
    return _job_dir(job_id) / "assets" / f"scene_{scene_index:03d}.mp4"


def _reset_scene_task_for_resubmit(job_id: str, scene_index: int, reason: str) -> None:
    metadata_path = _scene_task_path(job_id, scene_index)
    if not metadata_path.exists():
        return

    payload: dict[str, object] = {}
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}

    old_task_id = payload.pop("task_id", None)
    history = list(payload.get("retry_history", []))
    if old_task_id:
        history.append({"task_id": old_task_id, "error": reason})
    payload["retry_history"] = history
    payload["status"] = "reset_for_resubmit"
    payload["last_error"] = reason
    payload.pop("output_path", None)
    metadata_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _unstick_job(background_tasks: BackgroundTasks, job_id: str) -> JobRecord:
    job = _get_job(job_id)
    job_input = _get_job_input(job_id)

    if job.status == "completed" and job.output_url:
        raise HTTPException(status_code=400, detail="Completed jobs do not need unsticking.")

    reason = f"Reset locally at {_utc_now()} to force a fresh Kie submission for the next blocked scene."
    next_scene_to_reset: int | None = None
    for scene_index in range(1, max(job.scene_count, 0) + 1):
        if not _scene_clip_path(job_id, scene_index).exists():
            next_scene_to_reset = scene_index
            break

    if next_scene_to_reset is not None:
        _reset_scene_task_for_resubmit(job_id, next_scene_to_reset, reason)

    output_dir = BASE_BUILD_DIR / job_id
    _update_job(job_id, status="pending", error=None, output_url=None)
    background_tasks.add_task(
        _run_job,
        job_id,
        job_input.script_text,
        job_input.reference_url,
        output_dir,
        job_input.video_style,
        job_input.character,
    )
    return _get_job(job_id)


def _render_clip_audio_job(job_id: str) -> JobRecord:
    job = _get_job(job_id)
    clip_paths = _scene_clip_paths(job_id)
    if not clip_paths:
        raise HTTPException(status_code=400, detail="No scene clips found for this job yet.")

    output_path = _clip_audio_output_path(job_id, job.title)
    render_video_from_clips_with_clip_audio(
        clip_paths=clip_paths,
        output_path=output_path,
        width=1280,
        height=720,
        fps=24,
    )
    return _get_job(job_id)


def _run_clip_audio_job(job_id: str) -> None:
    try:
        _write_clip_audio_status(job_id, "rendering")
        _render_clip_audio_job(job_id)
        _write_clip_audio_status(job_id, "completed")
    except Exception as exc:
        _write_clip_audio_status(job_id, "failed", str(exc))


def _start_clip_audio_job(background_tasks: BackgroundTasks, job_id: str) -> JobRecord:
    job = _get_job(job_id)
    output_path = _clip_audio_output_path(job_id, job.title)
    if output_path.exists() and output_path.stat().st_size > 0:
        _write_clip_audio_status(job_id, "completed")
        return job

    status = _read_clip_audio_status(job_id)
    if status["status"] == "rendering" and _clip_audio_status_is_fresh(status):
        return job

    if not _scene_clip_paths(job_id):
        raise HTTPException(status_code=400, detail="No scene clips found for this job yet.")

    _write_clip_audio_status(job_id, "rendering")
    background_tasks.add_task(_run_clip_audio_job, job_id)
    return job


def _render_voice_test(sample_text: str) -> dict[str, str]:
    settings = Settings.from_env()
    clean_text = sample_text.strip() or "Hello, this is a voice test for the current ElevenLabs voice configuration."
    kie = KieClient(
        settings.kie_api_key,
        settings.kie_base_url,
        settings.kie_upload_base_url,
    )
    payload = {
        "text": clean_text,
        "voice": settings.kie_tts_voice,
        "stability": 0.5,
        "similarity_boost": 0.75,
        "style": 0,
        "speed": 1,
        "timestamps": False,
        "previous_text": "",
        "next_text": "",
        "language_code": "",
    }
    task_id = kie.create_task(settings.kie_tts_model, payload)
    result = kie.wait_for_task(task_id, timeout_seconds=2400)
    urls = find_urls(result)
    if not urls:
        raise RuntimeError(f"No narration URL found in Kie result: {json.dumps(result)}")

    voice_slug = "".join(ch if ch.isalnum() else "-" for ch in settings.kie_tts_voice.lower()).strip("-") or "voice"
    output_path = _voice_test_dir() / f"{voice_slug}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.mp3"
    kie.download_file(urls[0], output_path)
    return {
        "audio_url": _job_output_url(output_path),
        "voice": settings.kie_tts_voice,
        "text": clean_text,
    }


def _fallback_softened_narration(scene_offset: int) -> str:
    if scene_offset == 0:
        return "The situation takes an unexpected turn and does not go as planned."
    if scene_offset == 1:
        return "A serious setback changes the outcome and leaves a lasting lesson."
    return "The experience is difficult, but it encourages wiser choices going forward."


def _soften_narration_text(narration: str, scene_offset: int) -> str:
    softened = narration.strip().replace('"', "").replace("'", "")
    replacements = [
        (r"\bfool\b", "mistake"),
        (r"\bidiot\b", "poor decision-maker"),
        (r"\bkill(?:ed|ing)?\b", "stop"),
        (r"\bmurder(?:ed|ing)?\b", "harm"),
        (r"\bdead\b", "gone"),
        (r"\bdie(?:d|s|ing)?\b", "fade away"),
        (r"\bblood(?:y)?\b", "damage"),
        (r"\bweapon(?:s)?\b", "tools"),
        (r"\bgun(?:s)?\b", "device"),
        (r"\bknife\b", "tool"),
        (r"\bcrime\b", "mistake"),
        (r"\bcriminal(?:s)?\b", "wrongdoers"),
        (r"\bsteal(?:s|ing|t|en)?\b", "take away"),
        (r"\bscam(?:med|ming|s)?\b", "mislead"),
        (r"\bfraud(?:ulent)?\b", "dishonest"),
        (r"\bviolent(?:ly)?\b", "intensely"),
        (r"\bpain(?:ful)?\b", "difficulty"),
        (r"\btragic\b", "hard"),
    ]
    for pattern, replacement in replacements:
        softened = re.sub(pattern, replacement, softened, flags=re.IGNORECASE)

    softened = re.sub(r"[—–-]", " ", softened)
    softened = re.sub(r"\s+", " ", softened).strip(" .")

    flagged_terms = (
        "gone",
        "loss",
        "lost",
        "hurt",
        "harm",
        "danger",
        "desperate",
        "despair",
        "ruin",
        "ruined",
        "devastating",
        "destroyed",
    )
    if not softened or any(term in softened.lower() for term in flagged_terms):
        softened = _fallback_softened_narration(scene_offset)

    if not softened.endswith((".", "!", "?")):
        softened = f"{softened}."
    return softened


def _soften_blocked_scene_job(background_tasks: BackgroundTasks, job_id: str) -> JobRecord:
    job = _get_job(job_id)
    job_input = _get_job_input(job_id)

    if job.status == "completed" and job.output_url:
        raise HTTPException(status_code=400, detail="Completed jobs do not need blocked-scene softening.")

    plan = plan_from_script(job_input.script_text, video_style=job_input.video_style, character=job_input.character)
    next_scene_index: int | None = None
    for scene in plan.scenes:
        if not _scene_clip_path(job_id, scene.index).exists():
            next_scene_index = scene.index
            break

    if next_scene_index is None:
        raise HTTPException(status_code=400, detail="All scene clips already exist for this job.")

    updated_narrations = [scene.narration for scene in plan.scenes]
    changed_indices: list[int] = []
    for offset in range(3):
        scene_index = next_scene_index + offset
        if scene_index > len(updated_narrations):
            break
        updated_narrations[scene_index - 1] = _soften_narration_text(updated_narrations[scene_index - 1], offset)
        changed_indices.append(scene_index)

    rebuilt_script = f"# {plan.title}\n\n" + "\n\n---\n\n".join(updated_narrations)
    _save_job_input(
        job_id,
        JobInput(
            script_text=rebuilt_script,
            reference_url=job_input.reference_url,
            video_style=job_input.video_style,
            character=job_input.character,
        ),
    )

    reason = (
        f"Scene wording softened locally at {_utc_now()} for scene "
        f"{next_scene_index} and nearby beats to avoid provider safety blocks."
    )
    for scene_index in changed_indices:
        _reset_scene_task_for_resubmit(job_id, scene_index, reason)

    output_dir = BASE_BUILD_DIR / job_id
    _update_job(job_id, status="pending", error=reason, output_url=None)
    background_tasks.add_task(
        _run_job,
        job_id,
        rebuilt_script,
        job_input.reference_url,
        output_dir,
        job_input.video_style,
        job_input.character,
    )
    return _get_job(job_id)


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    sample_script = html.escape(
        "# Story Prompt\n\n"
        "Paste your narration script here.\n\n"
        "---\n\n"
        "Separate scenes with three dashes if you want more control."
    )
    style_options = "\n".join(
        f'<option value="{key}">{label}</option>'
        for key, label in [
            ("cinematic", "Cinematic"),
            ("anime", "Anime"),
            ("animation_3d", "3D Animation"),
            ("animation_2d", "2D Animation"),
            ("cartoon", "Cartoon"),
            ("realistic", "Realistic"),
            ("documentary", "Documentary"),
            ("futuristic_3d", "Futuristic 3D"),
        ]
    )
    character_options = "\n".join(
        f'<option value="{profile.key}">{profile.label}</option>'
        for profile in CHARACTER_PROFILES.values()
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YouTube Automation</title>
  <style>
    :root {{
      --bg: #f5efe4;
      --card: rgba(255,255,255,0.72);
      --ink: #182028;
      --muted: #56616b;
      --accent: #cb5a2e;
      --accent-dark: #7e3116;
      --line: rgba(24,32,40,0.12);
      --ok: #16774c;
      --warn: #9d5a07;
      --err: #a42828;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(203,90,46,0.22), transparent 28%),
        radial-gradient(circle at bottom right, rgba(22,119,76,0.15), transparent 24%),
        linear-gradient(135deg, #f8f1e8 0%, #efe7da 100%);
      min-height: 100vh;
    }}
    .shell {{
      width: min(1120px, calc(100% - 32px));
      margin: 32px auto;
      display: grid;
      grid-template-columns: 1.15fr 0.85fr;
      gap: 20px;
    }}
    .panel {{
      background: var(--card);
      backdrop-filter: blur(12px);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: 0 18px 55px rgba(37, 29, 17, 0.08);
      overflow: hidden;
    }}
    .hero {{
      padding: 28px;
      border-bottom: 1px solid var(--line);
      background:
        linear-gradient(135deg, rgba(203,90,46,0.14), rgba(255,255,255,0)),
        linear-gradient(180deg, rgba(255,255,255,0.32), rgba(255,255,255,0));
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: clamp(2rem, 4vw, 3.6rem);
      line-height: 0.96;
      letter-spacing: -0.04em;
      max-width: 10ch;
    }}
    p {{
      margin: 0;
      color: var(--muted);
      font-size: 1rem;
      line-height: 1.6;
    }}
    form {{
      padding: 24px 28px 28px;
      display: grid;
      gap: 18px;
    }}
    .split {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }}
    label {{
      display: grid;
      gap: 8px;
      font-size: 0.95rem;
      color: var(--ink);
    }}
    textarea, input[type="url"], input[type="text"], input[type="number"], select {{
      width: 100%;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.84);
      border-radius: 18px;
      padding: 14px 16px;
      font: inherit;
      color: inherit;
    }}
    textarea {{
      min-height: 320px;
      resize: vertical;
      line-height: 1.55;
    }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: 14px 20px;
      background: linear-gradient(135deg, var(--accent), var(--accent-dark));
      color: white;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      width: fit-content;
      box-shadow: 0 12px 24px rgba(126,49,22,0.2);
    }}
    button:disabled {{
      opacity: 0.6;
      cursor: wait;
    }}
    .stack {{
      display: grid;
      gap: 18px;
      padding: 20px;
    }}
    .card {{
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 18px;
      background: rgba(255,255,255,0.7);
    }}
    .eyebrow {{
      text-transform: uppercase;
      letter-spacing: 0.14em;
      font-size: 0.72rem;
      color: var(--muted);
      margin-bottom: 10px;
    }}
    .job-title {{
      margin: 0 0 8px;
      font-size: 1.3rem;
    }}
    .status {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      font-size: 0.9rem;
      font-weight: 700;
      background: rgba(24,32,40,0.06);
    }}
    .status.pending, .status.rendering {{ color: var(--warn); }}
    .status.completed {{ color: var(--ok); }}
    .status.failed {{ color: var(--err); }}
    .meta {{
      margin-top: 12px;
      color: var(--muted);
      line-height: 1.6;
      font-size: 0.95rem;
    }}
    .progress {{
      margin-top: 14px;
    }}
    .progress-track {{
      width: 100%;
      height: 10px;
      border-radius: 999px;
      background: rgba(24,32,40,0.08);
      overflow: hidden;
    }}
    .progress-fill {{
      height: 100%;
      background: linear-gradient(135deg, var(--accent), #e08b3a);
      border-radius: 999px;
      transition: width 0.3s ease;
    }}
    .progress-text {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    a.video-link {{
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      color: var(--accent-dark);
      font-weight: 700;
      text-decoration: none;
    }}
    .video-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
      margin-top: 14px;
    }}
    .download-link {{
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(203,90,46,0.12);
    }}
    .video-preview {{
      width: 100%;
      margin-top: 14px;
      border-radius: 16px;
      background: rgba(24,32,40,0.08);
      display: block;
      aspect-ratio: 16 / 9;
    }}
    .scene-preview {{
      margin-top: 14px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
    }}
    .scene-links {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }}
    .scene-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 42px;
      min-height: 34px;
      padding: 8px 10px;
      border-radius: 999px;
      background: rgba(24,32,40,0.08);
      color: var(--ink);
      font-size: 0.86rem;
      font-weight: 700;
      text-decoration: none;
    }}
    .empty {{
      color: var(--muted);
      font-style: italic;
    }}
    .button-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }}
    .secondary {{
      background: rgba(24,32,40,0.08);
      color: var(--ink);
      box-shadow: none;
    }}
    .tiny {{
      padding: 10px 14px;
      font-size: 0.9rem;
      margin-top: 14px;
    }}
    @media (max-width: 920px) {{
      .shell {{ grid-template-columns: 1fr; }}
      .split {{ grid-template-columns: 1fr; }}
      textarea {{ min-height: 240px; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="panel">
      <div class="hero">
        <div class="eyebrow">Local Studio</div>
        <h1>Build narrated YouTube videos from script + motion reference.</h1>
        <p>Generate a script with ChatGPT or paste your own, drop in a YouTube reference link, and let Kie generate scene videos while FastAPI tracks the job locally.</p>
      </div>
      <form id="job-form">
        <div class="card">
          <div class="eyebrow">Script Generator</div>
          <div class="split">
            <label>
              Topic
              <input id="script_topic" name="script_topic" type="text" placeholder="The rise of electric vehicles" />
            </label>
            <label>
              Amount of words
              <input id="target_words" name="target_words" type="number" min="80" max="5000" value="150" />
            </label>
          </div>
          <div class="split">
            <label>
              Angle
              <input id="script_angle" name="script_angle" type="text" placeholder="Focus on consumer adoption and charging" />
            </label>
            <label>
              Video style
              <select id="video_style" name="video_style">
                {style_options}
              </select>
            </label>
          </div>
          <div class="split">
            <label>
              African character
              <select id="character" name="character">
                {character_options}
              </select>
            </label>
            <label>
              Tone
              <select id="script_tone" name="script_tone">
                <option value="engaging">Engaging</option>
                <option value="cinematic">Cinematic</option>
                <option value="educational">Educational</option>
                <option value="urgent">Urgent</option>
                <option value="optimistic">Optimistic</option>
                <option value="storytelling">Storytelling</option>
              </select>
            </label>
          </div>
          <div class="button-row">
            <button id="generate-script-button" class="secondary" type="button">Generate Script With ChatGPT</button>
          </div>
        </div>
        <div class="card">
          <div class="eyebrow">Voice Test</div>
          <label>
            Voice test text
            <input id="voice_test_text" type="text" value="Hello, this is a voice test for the current ElevenLabs voice configuration." />
          </label>
          <div class="button-row">
            <button id="test-voice-button" class="secondary" type="button">Test Voice</button>
          </div>
          <div id="voice-test-result" class="meta"></div>
        </div>
        <label>
          Script
          <textarea id="script_text" name="script_text" required>{sample_script}</textarea>
        </label>
        <label>
          YouTube reference link
          <input id="reference_url" name="reference_url" type="url" placeholder="https://www.youtube.com/watch?v=..." required>
        </label>
        <button id="submit-button" type="submit">Start Render</button>
      </form>
    </section>
    <aside class="panel">
      <div class="hero">
        <div class="eyebrow">Jobs</div>
        <p>Each render runs in the background. Keep this page open to watch progress and open the final MP4 when it lands.</p>
      </div>
      <div class="stack" id="jobs"></div>
    </aside>
  </main>
  <script>
    const form = document.getElementById("job-form");
    const submitButton = document.getElementById("submit-button");
    const generateScriptButton = document.getElementById("generate-script-button");
    const testVoiceButton = document.getElementById("test-voice-button");
    const jobsRoot = document.getElementById("jobs");
    const scriptText = document.getElementById("script_text");
    const voiceTestResult = document.getElementById("voice-test-result");
    const jobs = new Map();

    function renderEmptyState() {{
      if (jobs.size > 0) return;
      jobsRoot.innerHTML = '<div class="card empty">No renders yet. Start one from the form on the left.</div>';
    }}

    function renderScenePreview(item) {{
      const clips = item.scene_clip_urls || [];
      const latestClip = clips[clips.length - 1];
      if (!latestClip || item.output_url) return "";

      return `
        <div class="scene-preview">
          <div class="eyebrow">Live Scene Preview</div>
          <video class="video-preview" controls muted preload="metadata" src="${{latestClip.url}}"></video>
          <div class="progress-text">Latest rendered scene: ${{latestClip.index}}</div>
          <div class="scene-links">
            ${{clips.map((clip) => `<a class="scene-link" href="${{clip.url}}" target="_blank" rel="noreferrer">S${{clip.index}}</a>`).join("")}}
          </div>
        </div>
      `;
    }}

    function latestSceneUrl(item) {{
      const clips = item.scene_clip_urls || [];
      return clips.length ? clips[clips.length - 1].url : "";
    }}

    function shouldRenderJob(previous, next) {{
      if (!previous) return true;
      return (
        previous.status !== next.status ||
        previous.title !== next.title ||
        previous.error !== next.error ||
        previous.output_url !== next.output_url ||
        previous.output_preview_url !== next.output_preview_url ||
        previous.clip_audio_output_url !== next.clip_audio_output_url ||
        previous.clip_audio_preview_url !== next.clip_audio_preview_url ||
        JSON.stringify(previous.clip_audio_status || {{}}) !== JSON.stringify(next.clip_audio_status || {{}}) ||
        previous.progress_current !== next.progress_current ||
        previous.progress_total !== next.progress_total ||
        latestSceneUrl(previous) !== latestSceneUrl(next)
      );
    }}

    function upsertJobCard(job) {{
      jobs.set(job.job_id, job);
      const ordered = Array.from(jobs.values()).sort((a, b) => a.created_at < b.created_at ? 1 : -1);
      jobsRoot.innerHTML = ordered.map((item) => `
        <article class="card">
          <div class="eyebrow">Job ${{item.job_id.slice(0, 8)}}</div>
          <h2 class="job-title">${{item.title || "Preparing render"}}</h2>
          <div class="status ${{item.status}}">${{item.status}}</div>
          <div class="meta">
            Scenes: ${{item.scene_count || 0}}<br>
            Updated: ${{new Date(item.updated_at).toLocaleString()}}
            ${{item.error ? `<br>Error: ${{item.error}}` : ""}}
          </div>
          ${{item.clip_audio_status && item.clip_audio_status.status === "rendering" ? `<div class="progress-text">Rendering clip-audio video...</div>` : ""}}
          ${{item.clip_audio_status && item.clip_audio_status.status === "failed" ? `<div class="progress-text">Clip-audio failed: ${{item.clip_audio_status.error || "Unknown error"}}</div>` : ""}}
          ${{(item.status === "pending" || item.status === "rendering") ? `
            <div class="progress">
              <div class="progress-track">
                <div class="progress-fill" style="width: ${{item.progress_percent || 0}}%"></div>
              </div>
              <div class="progress-text">${{item.progress_stage || "Working..."}} (${{item.progress_percent || 0}}%)</div>
              ${{item.progress_eta_label ? `<div class="progress-text">Estimated time remaining: ${{item.progress_eta_label}}</div>` : ""}}
            </div>
          ` : ""}}
          ${{renderScenePreview(item)}}
          ${{item.output_url ? `
            ${{item.output_preview_url ? `<video class="video-preview" controls preload="none" src="${{item.output_preview_url}}"></video>` : ""}}
            <div class="video-actions">
              <a class="video-link" href="${{item.output_url}}" target="_blank" rel="noreferrer">Open rendered video</a>
              <a class="video-link download-link" href="${{item.output_url}}" download>Download video</a>
            </div>
          ` : ""}}
          ${{item.clip_audio_output_url ? `
            ${{item.clip_audio_preview_url ? `<video class="video-preview" controls preload="none" src="${{item.clip_audio_preview_url}}"></video>` : ""}}
            <div class="video-actions">
              <a class="video-link" href="${{item.clip_audio_output_url}}" target="_blank" rel="noreferrer">Open clip-audio video</a>
              <a class="video-link download-link" href="${{item.clip_audio_output_url}}" download>Download clip-audio video</a>
            </div>
          ` : ""}}
          ${{item.status === "failed" ? `<div><button class="secondary tiny" type="button" onclick="retryJob('${{item.job_id}}')">Retry</button></div>` : ""}}
          ${{item.status !== "completed" ? `<div><button class="secondary tiny" type="button" onclick="softenSceneJob('${{item.job_id}}')">Soften Blocked Scene</button></div>` : ""}}
          ${{item.progress_current > 0 && (!item.clip_audio_status || item.clip_audio_status.status !== "rendering") ? `<div><button class="secondary tiny" type="button" onclick="renderClipAudioJob('${{item.job_id}}')">Render Clip Audio</button></div>` : ""}}
          ${{item.status !== "completed" ? `<div><button class="secondary tiny" type="button" onclick="unstickJob('${{item.job_id}}')">Unstick</button></div>` : ""}}
          ${{(item.status === "pending" || item.status === "rendering") ? `<div><button class="secondary tiny" type="button" onclick="cancelJob('${{item.job_id}}')">Cancel</button></div>` : ""}}
        </article>
      `).join("");
    }}

    async function fetchJob(jobId) {{
      const response = await fetch(`/api/jobs/${{jobId}}`);
      if (!response.ok) return;
      const job = await response.json();
      const previous = jobs.get(job.job_id);
      if (shouldRenderJob(previous, job)) {{
        upsertJobCard(job);
      }} else {{
        jobs.set(job.job_id, job);
      }}
      if (job.status === "pending" || job.status === "rendering") {{
        window.setTimeout(() => fetchJob(jobId), 4000);
      }}
    }}

    async function retryJob(jobId) {{
      const response = await fetch(`/api/jobs/${{jobId}}/retry`, {{
        method: "POST"
      }});

      const payload = await response.json().catch(() => ({{ detail: "Retry failed." }}));
      if (!response.ok) {{
        alert(payload.detail || "Could not retry the render.");
        return;
      }}

      upsertJobCard(payload);
      fetchJob(jobId);
    }}

    async function cancelJob(jobId) {{
      const response = await fetch(`/api/jobs/${{jobId}}/cancel`, {{
        method: "POST"
      }});

      const payload = await response.json().catch(() => ({{ detail: "Cancel failed." }}));
      if (!response.ok) {{
        alert(payload.detail || "Could not cancel the render.");
        return;
      }}

      upsertJobCard(payload);
      fetchJob(jobId);
    }}

    async function unstickJob(jobId) {{
      const response = await fetch(`/api/jobs/${{jobId}}/unstick`, {{
        method: "POST"
      }});

      const payload = await response.json().catch(() => ({{ detail: "Unstick failed." }}));
      if (!response.ok) {{
        alert(payload.detail || "Could not unstick the render.");
        return;
      }}

      upsertJobCard(payload);
      fetchJob(jobId);
    }}

    async function renderClipAudioJob(jobId) {{
      const response = await fetch(`/api/jobs/${{jobId}}/render-clip-audio`, {{
        method: "POST"
      }});

      const payload = await response.json().catch(() => ({{ detail: "Clip-audio render failed." }}));
      if (!response.ok) {{
        alert(payload.detail || "Could not render the clip-audio video.");
        return;
      }}

      upsertJobCard(payload);
    }}

    async function softenSceneJob(jobId) {{
      const response = await fetch(`/api/jobs/${{jobId}}/soften-blocked-scene`, {{
        method: "POST"
      }});

      const payload = await response.json().catch(() => ({{ detail: "Blocked-scene softening failed." }}));
      if (!response.ok) {{
        alert(payload.detail || "Could not soften the blocked scene.");
        return;
      }}

      upsertJobCard(payload);
      fetchJob(jobId);
    }}

    window.retryJob = retryJob;
    window.cancelJob = cancelJob;
    window.unstickJob = unstickJob;
    window.renderClipAudioJob = renderClipAudioJob;
    window.softenSceneJob = softenSceneJob;

    async function loadJobs() {{
      const response = await fetch("/api/jobs");
      if (!response.ok) {{
        renderEmptyState();
        return;
      }}

      const payload = await response.json();
      for (const job of payload.jobs || []) {{
        upsertJobCard(job);
        if (job.status === "pending" || job.status === "rendering") {{
          fetchJob(job.job_id);
        }}
      }}
      renderEmptyState();
    }}

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      submitButton.disabled = true;
      submitButton.textContent = "Starting...";

      const formData = new FormData(form);
      const response = await fetch("/api/jobs", {{
        method: "POST",
        body: formData
      }});

      submitButton.disabled = false;
      submitButton.textContent = "Start Render";

      if (!response.ok) {{
        const payload = await response.json().catch(() => ({{ detail: "Request failed." }}));
        alert(payload.detail || "Could not start the render.");
        return;
      }}

      const job = await response.json();
      upsertJobCard(job);
      fetchJob(job.job_id);
      form.reset();
    }});

    generateScriptButton.addEventListener("click", async () => {{
      const topic = document.getElementById("script_topic").value.trim();
      const angle = document.getElementById("script_angle").value.trim();
      const tone = document.getElementById("script_tone").value;
      const videoStyle = document.getElementById("video_style").value;
      const character = document.getElementById("character").value;
      const targetWords = document.getElementById("target_words").value;

      if (!topic) {{
        alert("Add a topic first.");
        return;
      }}

      generateScriptButton.disabled = true;
      generateScriptButton.textContent = "Generating...";

      const response = await fetch("/api/script/generate", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{
          topic,
          angle,
          tone,
          target_words: Number(targetWords || 150),
          video_style: videoStyle,
          character
        }})
      }});

      generateScriptButton.disabled = false;
      generateScriptButton.textContent = "Generate Script With ChatGPT";

      const payload = await response.json().catch(() => ({{ detail: "Request failed." }}));
      if (!response.ok) {{
        alert(payload.detail || "Could not generate the script.");
        return;
      }}

      scriptText.value = payload.script;
    }});

    testVoiceButton.addEventListener("click", async () => {{
      const text = document.getElementById("voice_test_text").value.trim();
      testVoiceButton.disabled = true;
      testVoiceButton.textContent = "Testing...";
      voiceTestResult.innerHTML = "";

      const response = await fetch("/api/voice/test", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ text }})
      }});

      testVoiceButton.disabled = false;
      testVoiceButton.textContent = "Test Voice";

      const payload = await response.json().catch(() => ({{ detail: "Voice test failed." }}));
      if (!response.ok) {{
        alert(payload.detail || "Could not test the voice.");
        return;
      }}

      voiceTestResult.innerHTML = `
        Voice: ${{payload.voice}}<br>
        <audio controls src="${{payload.audio_url}}" style="margin-top:10px; width:100%;"></audio><br>
        <a class="video-link" href="${{payload.audio_url}}" target="_blank" rel="noreferrer">Open voice test audio</a>
      `;
    }});

    loadJobs();
  </script>
</body>
</html>"""


@app.post("/api/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    script_text: str = Form(...),
    reference_url: str = Form(...),
    video_style: str = Form("cinematic"),
    character: str = Form("auto"),
) -> dict:
    if not script_text.strip():
        raise HTTPException(status_code=400, detail="Script text is required.")
    try:
        validated_reference_url = validate_youtube_url(reference_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = uuid4().hex
    created_at = _utc_now()
    output_dir = BASE_BUILD_DIR / job_id

    normalized_video_style = normalize_style(video_style)
    normalized_character = normalize_character_profile(character)

    try:
        plan = plan_from_script(script_text, video_style=normalized_video_style, character=normalized_character)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = JobRecord(
        job_id=job_id,
        title=plan.title,
        status="pending",
        created_at=created_at,
        updated_at=created_at,
        scene_count=len(plan.scenes),
    )
    _save_job(job)
    _save_job_input(
        job_id,
        JobInput(
            script_text=script_text,
            reference_url=validated_reference_url,
            video_style=normalized_video_style,
            character=normalized_character,
        ),
    )
    background_tasks.add_task(
        _run_job,
        job_id,
        script_text,
        validated_reference_url,
        output_dir,
        normalized_video_style,
        normalized_character,
    )
    return asdict(job)


@app.get("/api/jobs")
def list_jobs() -> dict:
    jobs: list[JobRecord] = []

    for path in sorted(BASE_BUILD_DIR.glob("*/job.json")):
        job_id = path.parent.name
        job = _read_job_record(job_id)
        if not job:
            continue
        jobs.append(job)
        with _jobs_lock:
            _jobs[job_id] = job

    jobs.sort(key=lambda item: item.created_at, reverse=True)
    return {"jobs": [_serialize_job(job) for job in jobs]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    return _serialize_job(_get_job(job_id))


@app.get("/api/jobs/{job_id}/preview/{kind}")
def preview_job_video(job_id: str, kind: str) -> FileResponse:
    job = _get_job(job_id)
    if kind == "rendered":
        source_path = _path_from_output_url(job.output_url)
    elif kind == "clip-audio":
        source_path = _clip_audio_output_path(job.job_id, job.title)
    else:
        raise HTTPException(status_code=404, detail="Unknown preview type.")

    if not source_path or not source_path.exists():
        raise HTTPException(status_code=404, detail="Preview source video not found.")

    preview_path = _ensure_streamable_preview(source_path)
    return FileResponse(preview_path, media_type="video/mp4")


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, background_tasks: BackgroundTasks) -> dict:
    return _serialize_job(_retry_job(background_tasks, job_id))


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    return _serialize_job(_cancel_job(job_id))


@app.post("/api/jobs/{job_id}/unstick")
def unstick_job(job_id: str, background_tasks: BackgroundTasks) -> dict:
    return _serialize_job(_unstick_job(background_tasks, job_id))


@app.post("/api/jobs/{job_id}/render-clip-audio")
def render_clip_audio_job(job_id: str, background_tasks: BackgroundTasks) -> dict:
    return _serialize_job(_start_clip_audio_job(background_tasks, job_id))


@app.post("/api/jobs/{job_id}/soften-blocked-scene")
def soften_blocked_scene_job(job_id: str, background_tasks: BackgroundTasks) -> dict:
    return _serialize_job(_soften_blocked_scene_job(background_tasks, job_id))


@app.post("/api/voice/test")
def test_voice(payload: VoiceTestRequest) -> dict:
    try:
        return _render_voice_test(payload.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/script/generate")
def generate_script(payload: ScriptRequest) -> dict:
    try:
        settings = Settings.from_env()
        generator = ScriptGenerator(settings)
        script = generator.generate_script(
            topic=payload.topic,
            angle=payload.angle,
            target_words=payload.target_words,
            tone=payload.tone,
            video_style=payload.video_style,
            character=payload.character,
        )
        return {"script": script}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/kie/callback")
async def kie_callback(request: Request) -> JSONResponse:
    payload = await request.json()
    task_id = str((payload.get("data") or {}).get("taskId") or "").strip()
    if not task_id:
        return JSONResponse({"ok": True, "matched": False})

    match = _find_task_metadata(task_id)
    if not match:
        return JSONResponse({"ok": True, "matched": False, "task_id": task_id})

    job_id, metadata_path = match
    _append_callback_log(job_id, payload)
    _update_task_metadata_from_callback(job_id, metadata_path, payload)
    return JSONResponse({"ok": True, "matched": True, "job_id": job_id, "task_id": task_id})
