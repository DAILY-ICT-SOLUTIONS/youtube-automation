from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CharacterProfile:
    key: str
    label: str
    script_direction: str
    visual_direction: str


CHARACTER_PROFILES: dict[str, CharacterProfile] = {
    "auto": CharacterProfile(
        key="auto",
        label="Auto / Script-led",
        script_direction="Let the topic decide the narrator and on-screen subject.",
        visual_direction="Let the script decide the main subject; keep casting consistent within each scene.",
    ),
    "african_female_nigerian": CharacterProfile(
        key="african_female_nigerian",
        label="African Female - Nigerian",
        script_direction=(
            "Frame the story around an African female lead or narrator where a character is needed. "
            "Use contemporary Nigerian context when it naturally fits the topic."
        ),
        visual_direction=(
            "Use a consistent adult African female lead with Nigerian styling cues, warm expressive presence, "
            "natural skin texture, modern wardrobe, and respectful culturally grounded details."
        ),
    ),
    "african_male_nigerian": CharacterProfile(
        key="african_male_nigerian",
        label="African Male - Nigerian",
        script_direction=(
            "Frame the story around an African male lead or narrator where a character is needed. "
            "Use contemporary Nigerian context when it naturally fits the topic."
        ),
        visual_direction=(
            "Use a consistent adult African male lead with Nigerian styling cues, confident expressive presence, "
            "natural skin texture, modern wardrobe, and respectful culturally grounded details."
        ),
    ),
    "african_female_pan": CharacterProfile(
        key="african_female_pan",
        label="African Female - Pan-African",
        script_direction=(
            "Frame the story around an African female lead or narrator where a character is needed. "
            "Keep the setting broadly African unless the topic names a specific country."
        ),
        visual_direction=(
            "Use a consistent adult African female lead, elegant contemporary African styling, natural skin texture, "
            "expressive face, modern wardrobe, and respectful region-neutral details."
        ),
    ),
    "african_male_pan": CharacterProfile(
        key="african_male_pan",
        label="African Male - Pan-African",
        script_direction=(
            "Frame the story around an African male lead or narrator where a character is needed. "
            "Keep the setting broadly African unless the topic names a specific country."
        ),
        visual_direction=(
            "Use a consistent adult African male lead, polished contemporary African styling, natural skin texture, "
            "expressive face, modern wardrobe, and respectful region-neutral details."
        ),
    ),
}


def normalize_character_profile(value: str | None) -> str:
    key = (value or "auto").strip()
    return key if key in CHARACTER_PROFILES else "auto"


def character_profile(value: str | None) -> CharacterProfile:
    return CHARACTER_PROFILES[normalize_character_profile(value)]
