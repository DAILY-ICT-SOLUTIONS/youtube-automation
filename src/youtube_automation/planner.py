from __future__ import annotations

from pathlib import Path
import re

from .models import Scene, VideoPlan
from .styles import normalize_style, style_prompt

MAX_WORDS_PER_SCENE = 16
MIN_WORDS_PER_SCENE = 7
TARGET_SCENE_DURATION_SECONDS = 5


def _clean_block(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [part.strip() for part in parts if part.strip()]


def _chunk_sentences(sentences: list[str], chunk_size: int = 2) -> list[str]:
    chunks: list[str] = []
    for index in range(0, len(sentences), chunk_size):
        chunks.append(" ".join(sentences[index:index + chunk_size]))
    return chunks


def _word_count(text: str) -> int:
    return len(text.split())


def _split_long_sentence(sentence: str, max_words: int = MAX_WORDS_PER_SCENE) -> list[str]:
    words = sentence.split()
    if len(words) <= max_words:
        return [sentence.strip()]

    parts: list[str] = []
    for index in range(0, len(words), max_words):
        parts.append(" ".join(words[index:index + max_words]).strip())
    return [part for part in parts if part]


def _rebalance_blocks(blocks: list[str]) -> list[str]:
    normalized_blocks: list[str] = []
    for block in blocks:
        sentences = _split_sentences(block)
        if not sentences:
            continue

        current_parts: list[str] = []
        current_words = 0
        for sentence in sentences:
            for sentence_part in _split_long_sentence(sentence):
                sentence_words = _word_count(sentence_part)
                if current_parts and current_words + sentence_words > MAX_WORDS_PER_SCENE:
                    normalized_blocks.append(" ".join(current_parts).strip())
                    current_parts = [sentence_part]
                    current_words = sentence_words
                else:
                    current_parts.append(sentence_part)
                    current_words += sentence_words

        if current_parts:
            normalized_blocks.append(" ".join(current_parts).strip())

    if len(normalized_blocks) < 2:
        return normalized_blocks

    merged_blocks: list[str] = []
    for block in normalized_blocks:
        if merged_blocks:
            previous = merged_blocks[-1]
            if _word_count(previous) < MIN_WORDS_PER_SCENE and _word_count(previous) + _word_count(block) <= MAX_WORDS_PER_SCENE:
                merged_blocks[-1] = f"{previous} {block}".strip()
                continue
        merged_blocks.append(block)

    return merged_blocks


def _build_visual_prompt(title: str, narration: str, video_style: str) -> str:
    return (
        f"High-quality short-form AI video scene about '{title}'. "
        f"Visual style: {style_prompt(video_style)}. "
        f"Scene content: {narration} "
        "Natural motion, clear subject, strong composition, expressive camera movement, "
        "high production value, clean frame, no text overlay, suitable for premium text-to-video generation."
    )


def plan_from_script(script_text: str, video_style: str = "cinematic") -> VideoPlan:
    lines = script_text.strip().splitlines()
    title = "Untitled Video"
    normalized_style = normalize_style(video_style)

    if lines and lines[0].lstrip().startswith("#"):
        title = lines[0].lstrip("#").strip() or title
        body = "\n".join(lines[1:]).strip()
    else:
        body = script_text.strip()

    raw_blocks = [block.strip() for block in re.split(r"\n\s*---+\s*\n", body) if block.strip()]

    if not raw_blocks:
        raise ValueError("The script file is empty.")

    if len(raw_blocks) == 1:
        sentences = _split_sentences(raw_blocks[0])
        raw_blocks = _chunk_sentences(sentences, chunk_size=2) or [raw_blocks[0]]

    raw_blocks = _rebalance_blocks(raw_blocks)

    scenes: list[Scene] = []
    narration_parts: list[str] = []

    for index, block in enumerate(raw_blocks, start=1):
        narration = _clean_block(block)
        narration_parts.append(narration)
        scenes.append(
            Scene(
                index=index,
                narration=narration,
                visual_prompt=_build_visual_prompt(title, narration, normalized_style),
                target_duration_seconds=TARGET_SCENE_DURATION_SECONDS,
            )
        )

    return VideoPlan(
        title=title,
        full_narration=" ".join(narration_parts),
        scenes=scenes,
        video_style=normalized_style,
    )


def plan_from_file(script_file: Path, video_style: str = "cinematic") -> VideoPlan:
    return plan_from_script(script_file.read_text(encoding="utf-8"), video_style=video_style)
