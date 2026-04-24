from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Scene:
    index: int
    narration: str
    visual_prompt: str
    target_duration_seconds: int = 5


@dataclass(slots=True)
class SceneAsset:
    scene: Scene
    image_path: Path
    start_time: float
    end_time: float


@dataclass(slots=True)
class VideoPlan:
    title: str
    full_narration: str
    scenes: list[Scene]
    video_style: str = "cinematic"
