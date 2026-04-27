from __future__ import annotations

from pathlib import Path
import json
import shlex
import subprocess

from .models import SceneAsset


def run_command(args: list[str], timeout_seconds: int | None = None) -> None:
    try:
        completed = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Command timed out:\n"
            f"{shlex.join(args)}\n\n"
            f"stdout:\n{exc.stdout or ''}\n\nstderr:\n{exc.stderr or ''}"
        ) from exc

    if completed.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            f"{shlex.join(args)}\n\n"
            f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
        )


def probe_duration(media_path: Path) -> float:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(media_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {media_path}:\n{completed.stderr}")

    payload = json.loads(completed.stdout)
    return float(payload["format"]["duration"])


def is_valid_video(media_path: Path) -> bool:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            str(media_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return False

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return False

    streams = payload.get("streams") or []
    return any(stream.get("codec_type") == "video" for stream in streams)


def write_srt(scene_assets: list[SceneAsset], subtitle_path: Path) -> Path:
    subtitle_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    for index, asset in enumerate(scene_assets, start=1):
        lines.extend(
            [
                str(index),
                f"{_format_srt_time(asset.start_time)} --> {_format_srt_time(asset.end_time)}",
                asset.scene.narration,
                "",
            ]
        )

    subtitle_path.write_text("\n".join(lines), encoding="utf-8")
    return subtitle_path


def build_scene_assets(scene_image_paths: list[Path], narrations: list[str], audio_duration: float) -> list[SceneAsset]:
    from .models import Scene, SceneAsset  # local import to avoid cycles

    if len(scene_image_paths) != len(narrations):
        raise ValueError("Scene image count must match narration count.")

    weights = [max(len(text.split()), 1) for text in narrations]
    total_weight = sum(weights)

    assets: list[SceneAsset] = []
    cursor = 0.0

    for index, (image_path, narration, weight) in enumerate(zip(scene_image_paths, narrations, weights), start=1):
        duration = audio_duration * (weight / total_weight)
        start_time = cursor
        end_time = audio_duration if index == len(narrations) else cursor + duration
        cursor = end_time
        assets.append(
            SceneAsset(
                scene=Scene(index=index, narration=narration, visual_prompt=""),
                image_path=image_path,
                start_time=start_time,
                end_time=end_time,
            )
        )

    return assets


def render_video(
    *,
    scene_assets: list[SceneAsset],
    audio_path: Path,
    subtitle_path: Path,
    output_path: Path,
    width: int,
    height: int,
    fps: int,
) -> Path:
    working_dir = output_path.parent / "segments"
    working_dir.mkdir(parents=True, exist_ok=True)

    segment_paths: list[Path] = []
    for asset in scene_assets:
        duration = max(asset.end_time - asset.start_time, 0.1)
        segment_path = working_dir / f"scene_{asset.scene.index:03d}.mp4"
        filter_chain = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            "zoompan=z='min(zoom+0.0008,1.12)':d=1:s="
            f"{width}x{height}:fps={fps}"
        )
        run_command(
            [
                "ffmpeg",
                "-y",
                "-loop",
                "1",
                "-i",
                str(asset.image_path),
                "-vf",
                filter_chain,
                "-t",
                f"{duration:.3f}",
                "-r",
                str(fps),
                "-pix_fmt",
                "yuv420p",
                str(segment_path),
            ]
        )
        segment_paths.append(segment_path)

    concat_list_path = output_path.parent / "concat.txt"
    concat_lines = [f"file '{path.resolve().as_posix()}'" for path in segment_paths]
    concat_list_path.write_text("\n".join(concat_lines), encoding="utf-8")

    visual_only_path = output_path.parent / "visual.mp4"
    run_command(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list_path),
            "-c",
            "copy",
            str(visual_only_path),
        ]
    )

    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(visual_only_path),
            "-i",
            str(audio_path),
            "-i",
            str(subtitle_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map",
            "2:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-c:s",
            "mov_text",
            "-shortest",
            str(output_path),
        ]
    )

    return output_path


def render_video_from_clips(
    *,
    clip_paths: list[Path],
    audio_path: Path,
    subtitle_path: Path,
    output_path: Path,
    width: int,
    height: int,
    fps: int,
) -> Path:
    if not clip_paths:
        raise ValueError("At least one clip is required to render a video.")

    working_dir = output_path.parent / "clips_normalized"
    working_dir.mkdir(parents=True, exist_ok=True)

    normalized_paths: list[Path] = []
    for index, clip_path in enumerate(clip_paths, start=1):
        if not is_valid_video(clip_path):
            raise RuntimeError(f"Clip is invalid and must be regenerated before rendering: {clip_path}")
        normalized_path = working_dir / f"clip_{index:03d}.mp4"
        run_command(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-i",
                str(clip_path),
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps}",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(normalized_path),
            ]
        )
        normalized_paths.append(normalized_path)

    concat_list_path = output_path.parent / "concat.txt"
    concat_lines = [f"file '{path.resolve().as_posix()}'" for path in normalized_paths]
    concat_list_path.write_text("\n".join(concat_lines), encoding="utf-8")

    visual_only_path = output_path.parent / "visual.mp4"
    run_command(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list_path),
            "-c",
            "copy",
            str(visual_only_path),
        ]
    )

    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(visual_only_path),
            "-i",
            str(audio_path),
            "-i",
            str(subtitle_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map",
            "2:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-c:s",
            "mov_text",
            "-shortest",
            str(output_path),
        ]
    )

    return output_path


def render_video_from_clips_with_clip_audio(
    *,
    clip_paths: list[Path],
    output_path: Path,
    width: int,
    height: int,
    fps: int,
) -> Path:
    if not clip_paths:
        raise ValueError("At least one clip is required to render a video.")

    working_dir = output_path.parent / "clip_audio_join" / "clips_normalized"
    working_dir.mkdir(parents=True, exist_ok=True)

    normalized_paths: list[Path] = []
    for index, clip_path in enumerate(clip_paths, start=1):
        normalized_path = working_dir / f"clip_{index:03d}.mp4"
        run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(clip_path),
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                str(normalized_path),
            ],
            timeout_seconds=240,
        )
        normalized_paths.append(normalized_path)

    concat_list_path = output_path.parent / "clip_audio_join" / "concat.txt"
    concat_lines = [f"file '{path.resolve().as_posix()}'" for path in normalized_paths]
    concat_list_path.write_text("\n".join(concat_lines), encoding="utf-8")

    run_command(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list_path),
            "-c",
            "copy",
            str(output_path),
        ],
        timeout_seconds=300,
    )

    return output_path


def _format_srt_time(value: float) -> str:
    total_milliseconds = max(int(round(value * 1000)), 0)
    milliseconds = total_milliseconds % 1000
    total_seconds = total_milliseconds // 1000
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"
