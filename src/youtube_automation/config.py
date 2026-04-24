from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


def load_dotenv(dotenv_path: Path = Path(".env")) -> None:
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(slots=True)
class Settings:
    kie_api_key: str
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    kie_base_url: str = "https://api.kie.ai"
    kie_upload_base_url: str = "https://kieai.redpandaai.co"
    kie_callback_url: str | None = None
    kie_video_model: str = "veo3_fast"
    kie_tts_model: str = "elevenlabs/text-to-speech-turbo-2-5"
    kie_tts_voice: str = "EiNlNiXeDU1pqqOPrYMO"
    kie_video_resolution: str = "480p"
    veo_retry_attempts: int = 3
    veo_retry_backoff_seconds: int = 45
    video_width: int = 1920
    video_height: int = 1080
    video_fps: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        api_key = os.environ.get("KIE_API_KEY", "").strip()
        if not api_key:
            raise ValueError("KIE_API_KEY is required. Add it to your environment or .env file.")

        return cls(
            kie_api_key=api_key,
            openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip() or None,
            openai_model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini",
            kie_base_url=os.environ.get("KIE_BASE_URL", "https://api.kie.ai").rstrip("/"),
            kie_upload_base_url=os.environ.get("KIE_UPLOAD_BASE_URL", "https://kieai.redpandaai.co").rstrip("/"),
            kie_callback_url=os.environ.get("KIE_CALLBACK_URL", "").strip() or None,
            kie_video_model=os.environ.get("KIE_VIDEO_MODEL", "veo3_fast"),
            kie_tts_model=os.environ.get("KIE_TTS_MODEL", "elevenlabs/text-to-speech-turbo-2-5"),
            kie_tts_voice=os.environ.get("KIE_TTS_VOICE", "EiNlNiXeDU1pqqOPrYMO"),
            kie_video_resolution=os.environ.get("KIE_VIDEO_RESOLUTION", "480p"),
            veo_retry_attempts=int(os.environ.get("VEO_RETRY_ATTEMPTS", "3")),
            veo_retry_backoff_seconds=int(os.environ.get("VEO_RETRY_BACKOFF_SECONDS", "45")),
            video_width=int(os.environ.get("VIDEO_WIDTH", "1920")),
            video_height=int(os.environ.get("VIDEO_HEIGHT", "1080")),
            video_fps=int(os.environ.get("VIDEO_FPS", "30")),
        )
