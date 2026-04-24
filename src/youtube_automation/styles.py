from __future__ import annotations


STYLE_PRESETS: dict[str, str] = {
    "cinematic": "cinematic live-action, photorealistic, dramatic lighting, polished film look",
    "anime": "anime style, expressive character design, dynamic framing, rich cel shading",
    "animation_3d": "stylized 3D animation, polished rendering, dimensional characters and environments",
    "animation_2d": "2D animated look, illustrated frames, bold shapes, clean motion design",
    "cartoon": "cartoon style, playful exaggerated forms, colorful animated world",
    "realistic": "highly realistic live-action look, natural textures, grounded details",
    "documentary": "documentary realism, editorial composition, naturalistic environments",
    "futuristic_3d": "futuristic 3D visuals, sleek surfaces, advanced technology aesthetics",
}


def normalize_style(style: str | None) -> str:
    candidate = (style or "cinematic").strip().lower().replace("-", "_").replace(" ", "_")
    return candidate if candidate in STYLE_PRESETS else "cinematic"


def style_prompt(style: str | None) -> str:
    return STYLE_PRESETS[normalize_style(style)]
