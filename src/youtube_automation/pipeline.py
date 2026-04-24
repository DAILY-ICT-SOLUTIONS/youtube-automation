from __future__ import annotations

from pathlib import Path
from datetime import datetime, UTC
import json
import re
import time

from .config import Settings
from .ffmpeg import build_scene_assets, is_valid_video, probe_duration, render_video_from_clips, write_srt
from .kie import KieClient, find_urls
from .models import VideoPlan
from .reference_video import download_reference_video


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "video"


class VideoPipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.kie = KieClient(
            settings.kie_api_key,
            settings.kie_base_url,
            settings.kie_upload_base_url,
        )

    def create_video(self, plan: VideoPlan, reference_source: str, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        assets_dir = output_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        inputs_dir = output_dir / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)

        reference_video_url = None
        if self.settings.kie_video_model.startswith("bytedance/"):
            reference_video = download_reference_video(reference_source, inputs_dir)
            reference_video_url = self.kie.upload_file(reference_video)
        audio_path = self._generate_narration(plan, assets_dir)
        clip_paths = self._generate_scene_videos(plan, reference_video_url, assets_dir)

        audio_duration = probe_duration(audio_path)
        scene_assets = build_scene_assets(
            clip_paths,
            [scene.narration for scene in plan.scenes],
            audio_duration,
        )

        subtitle_path = write_srt(scene_assets, output_dir / "subtitles.srt")
        output_path = output_dir / f"{slugify(plan.title)}.mp4"

        final_path = render_video_from_clips(
            clip_paths=clip_paths,
            audio_path=audio_path,
            subtitle_path=subtitle_path,
            output_path=output_path,
            width=self.settings.video_width,
            height=self.settings.video_height,
            fps=self.settings.video_fps,
        )
        self._write_publish_log(
            plan=plan,
            reference_source=reference_source,
            output_dir=output_dir,
            output_path=final_path,
            audio_duration=audio_duration,
            clip_paths=clip_paths,
        )
        return final_path

    def _generate_narration(self, plan: VideoPlan, assets_dir: Path) -> Path:
        audio_path = assets_dir / "narration.mp3"
        if audio_path.exists() and audio_path.stat().st_size > 0:
            return audio_path

        payload = {
            "text": plan.full_narration,
            "voice": self.settings.kie_tts_voice,
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0,
            "speed": 1,
            "timestamps": False,
            "previous_text": "",
            "next_text": "",
            "language_code": "",
        }
        task_id = self._get_or_create_task_id(
            metadata_path=assets_dir / "narration.task.json",
            model=self.settings.kie_tts_model,
            input_payload=payload,
        )
        result = self.kie.wait_for_task(task_id, timeout_seconds=2400)
        urls = find_urls(result)
        if not urls:
            raise RuntimeError(f"No narration URL found in Kie result: {json.dumps(result)}")

        downloaded = self.kie.download_file(urls[0], audio_path)
        self._mark_task_completed(assets_dir / "narration.task.json", downloaded)
        return downloaded

    def _generate_scene_videos(self, plan: VideoPlan, reference_video_url: str | None, assets_dir: Path) -> list[Path]:
        video_paths: list[Path] = []

        for scene in plan.scenes:
            destination = assets_dir / f"scene_{scene.index:03d}.mp4"
            metadata_path = assets_dir / f"scene_{scene.index:03d}.task.json"
            if destination.exists() and destination.stat().st_size > 0 and is_valid_video(destination):
                video_paths.append(destination)
                continue
            if destination.exists() and destination.stat().st_size > 0:
                self._mark_task_for_retry(
                    metadata_path,
                    f"Discarded invalid local clip for scene {scene.index}; ffprobe could not read the video stream.",
                    reset_task_id=True,
                )

            task_kind = "market"
            if self.settings.kie_video_model.startswith("veo3"):
                task_id, result = self._generate_veo_scene_with_backoff(
                    metadata_path=metadata_path,
                    prompt=scene.visual_prompt,
                    model=self.settings.kie_video_model,
                )
                task_kind = "veo"
            else:
                payload = {
                    "prompt": scene.visual_prompt,
                    "reference_video_urls": [reference_video_url] if reference_video_url else [],
                    "resolution": self.settings.kie_video_resolution,
                    "aspect_ratio": "16:9",
                    "duration": _scene_duration_seconds(scene),
                    "generate_audio": False,
                    "web_search": False,
                }
                task_id = self._get_or_create_task_id(
                    metadata_path=metadata_path,
                    model=self.settings.kie_video_model,
                    input_payload=payload,
                )
                result = self.kie.wait_for_task(task_id, timeout_seconds=2400, task_kind=task_kind)
            urls = find_urls(result)
            if not urls:
                raise RuntimeError(f"No video URL found for scene {scene.index}: {json.dumps(result)}")

            download_url = urls[0]
            if task_kind == "veo":
                upgraded_url = self.kie.try_get_veo_1080p_video_url(task_id)
                if upgraded_url:
                    download_url = upgraded_url

            downloaded = self.kie.download_file(download_url, destination)
            if not is_valid_video(downloaded):
                self._mark_task_for_retry(
                    metadata_path,
                    f"Downloaded clip for scene {scene.index} was invalid and will be regenerated.",
                    reset_task_id=True,
                )
                raise RuntimeError(
                    f"Downloaded clip for scene {scene.index} is invalid. Retry the job to regenerate that scene."
                )
            self._mark_task_completed(metadata_path, downloaded)
            video_paths.append(downloaded)

        return video_paths

    def _get_or_create_task_id(self, *, metadata_path: Path, model: str, input_payload: dict) -> str:
        if metadata_path.exists():
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            existing_task_id = payload.get("task_id")
            if existing_task_id:
                return existing_task_id

        task_id = self.kie.create_task(model, input_payload)
        metadata_path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "model": model,
                    "status": "submitted",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return task_id

    def _mark_task_completed(self, metadata_path: Path, output_path: Path) -> None:
        payload: dict[str, str] = {}
        if metadata_path.exists():
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload["status"] = "completed"
        payload["output_path"] = str(output_path)
        metadata_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _mark_task_for_retry(self, metadata_path: Path, error_message: str, *, reset_task_id: bool) -> None:
        payload: dict[str, object] = {}
        if metadata_path.exists():
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))

        history = list(payload.get("retry_history", []))
        existing_task_id = payload.get("task_id")
        if existing_task_id:
            history.append({"task_id": existing_task_id, "error": error_message})
        payload["retry_history"] = history
        payload["status"] = "retrying"
        payload["last_error"] = error_message
        if reset_task_id:
            payload.pop("task_id", None)
        payload.pop("output_path", None)
        metadata_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _get_or_create_veo_task_id(self, *, metadata_path: Path, prompt: str, model: str) -> str:
        if metadata_path.exists():
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            existing_task_id = payload.get("task_id")
            if existing_task_id:
                return existing_task_id

        task_id = self.kie.create_veo_task(
            prompt=prompt,
            model=model,
            aspect_ratio="16:9",
            resolution=self.settings.kie_video_resolution,
            callback_url=self.settings.kie_callback_url,
        )
        metadata_path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "model": model,
                    "status": "submitted",
                    "task_kind": "veo",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return task_id

    def _generate_veo_scene_with_backoff(self, *, metadata_path: Path, prompt: str, model: str) -> tuple[str, dict]:
        max_attempts = max(self.settings.veo_retry_attempts, 1)
        last_error: Exception | None = None

        for attempt in range(max_attempts):
            try:
                task_id = self._get_or_create_veo_task_id(
                    metadata_path=metadata_path,
                    prompt=prompt,
                    model=model,
                )
                result = self.kie.wait_for_task(task_id, timeout_seconds=2400, task_kind="veo")
                return task_id, result
            except Exception as exc:
                last_error = exc
                if not self._is_transient_veo_error(exc) or attempt >= max_attempts - 1:
                    raise

                reset_task_id = not isinstance(exc, TimeoutError)
                self._mark_task_for_retry(metadata_path, str(exc), reset_task_id=reset_task_id)
                backoff_seconds = self.settings.veo_retry_backoff_seconds * (attempt + 1)
                time.sleep(backoff_seconds)

        if last_error is not None:
            raise last_error
        raise RuntimeError("Veo scene generation failed without an error.")

    def _is_transient_veo_error(self, error: Exception) -> bool:
        message = str(error).lower()
        return (
            "internal error" in message
            or "please try again later" in message
            or "timed out waiting for kie task" in message
            or "ssl handshake operation timed out" in message
            or "connection reset by peer" in message
        )

    def _write_publish_log(
        self,
        *,
        plan: VideoPlan,
        reference_source: str,
        output_dir: Path,
        output_path: Path,
        audio_duration: float,
        clip_paths: list[Path],
    ) -> None:
        total_words = len(plan.full_narration.split())
        estimated_cost = _estimate_cost(scene_count=len(plan.scenes), total_words=total_words)
        payload = {
            "created_at": datetime.now(UTC).isoformat(),
            "topic": plan.title,
            "title": plan.title,
            "description": f"Generated from automated scene-first pipeline using {len(plan.scenes)} low-cost clips.",
            "hashtags": _hashtags_from_title(plan.title),
            "video_style": plan.video_style,
            "scene_count": len(plan.scenes),
            "total_words": total_words,
            "target_clip_duration_seconds": 5,
            "reference_source": reference_source,
            "duration_seconds": round(audio_duration, 2),
            "file_size_bytes": output_path.stat().st_size if output_path.exists() else 0,
            "video_file": output_path.name,
            "clip_files": [path.name for path in clip_paths],
            "cost_breakdown": estimated_cost,
        }
        (output_dir / "publish_log.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _scene_duration_seconds(scene) -> int:
    words = max(len(scene.narration.split()), 1)
    if words <= 10:
        return 4
    if words <= 16:
        return 5
    return 6


def _estimate_cost(*, scene_count: int, total_words: int) -> dict[str, float]:
    tts_cost = round(max(total_words, 1) * 0.00006, 3)
    video_cost = round(scene_count * 0.05 * 5, 3)
    total = round(tts_cost + video_cost, 3)
    return {
        "video_generation_usd": video_cost,
        "tts_generation_usd": tts_cost,
        "estimated_total_usd": total,
    }


def _hashtags_from_title(title: str) -> list[str]:
    words = [re.sub(r"[^a-zA-Z0-9]", "", part) for part in title.split()]
    tags = [f"#{word}" for word in words if word][:4]
    return tags or ["#YouTubeAutomation"]
