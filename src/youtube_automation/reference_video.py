from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
import json
import subprocess


YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


def validate_youtube_url(url: str) -> str:
    normalized = url.strip()
    if not normalized:
        raise ValueError("A YouTube video link is required.")

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in YOUTUBE_HOSTS:
        raise ValueError("Please provide a valid YouTube video URL.")

    return normalized


def download_reference_video(url: str, destination_dir: Path) -> Path:
    validated = validate_youtube_url(url)
    destination_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(destination_dir / "reference.%(ext)s")

    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed. Run `pip install -e .` again.") from exc

    options = {
        "format": "mp4/bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(validated, download=True)
        downloaded_path = Path(downloader.prepare_filename(info))

    if downloaded_path.suffix != ".mp4":
        mp4_candidate = downloaded_path.with_suffix(".mp4")
        if mp4_candidate.exists():
            downloaded_path = mp4_candidate

    if not downloaded_path.exists():
        matches = sorted(destination_dir.glob("reference.*"))
        if not matches:
            raise RuntimeError("Could not find the downloaded YouTube reference video.")
        downloaded_path = matches[0]

    return ensure_max_duration(downloaded_path, max_seconds=15, destination_dir=destination_dir)


def ensure_max_duration(source: Path, max_seconds: int, destination_dir: Path) -> Path:
    duration = probe_duration(source)
    if duration <= max_seconds:
        return source

    clipped_path = destination_dir / f"{source.stem}_trimmed.mp4"
    copy_result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-t",
            str(max_seconds),
            "-c",
            "copy",
            str(clipped_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if copy_result.returncode == 0 and clipped_path.exists():
        return clipped_path

    reencode_result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-t",
            str(max_seconds),
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(clipped_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if reencode_result.returncode != 0 or not clipped_path.exists():
        raise RuntimeError(
            "Could not trim the YouTube reference video to 15 seconds.\n"
            f"ffmpeg stderr:\n{reencode_result.stderr or copy_result.stderr}"
        )

    return clipped_path


def probe_duration(source: Path) -> float:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Could not inspect reference video duration:\n{completed.stderr}")

    payload = json.loads(completed.stdout)
    return float(payload["format"]["duration"])
