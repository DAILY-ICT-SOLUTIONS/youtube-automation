from __future__ import annotations

from openai import NotFoundError, OpenAI

from .characters import character_profile
from .config import Settings
from .styles import style_prompt


class ScriptGenerator:
    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required to generate scripts with ChatGPT.")

        self.client = OpenAI(api_key=settings.openai_api_key)
        self.model = settings.openai_model
        self.fallback_model = "gpt-4.1-mini"

    def generate_script(
        self,
        *,
        topic: str,
        angle: str = "",
        target_words: int = 150,
        tone: str = "engaging",
        video_style: str = "cinematic",
        character: str = "auto",
    ) -> str:
        clean_topic = topic.strip()
        if not clean_topic:
            raise ValueError("A topic is required to generate a script.")

        target_words = max(80, min(target_words, 5000))
        profile = character_profile(character)

        prompt = (
            "Write a YouTube narration script that is optimized for low-cost short scene generation.\n"
            f"Topic: {clean_topic}\n"
            f"Angle: {angle.strip() or 'Give the viewer a clear, compelling overview.'}\n"
            f"Tone: {tone.strip() or 'engaging'}\n"
            f"Preferred visual style: {style_prompt(video_style)}\n"
            f"Character direction: {profile.script_direction}\n"
            f"Target word count: about {target_words} words\n\n"
            "Format requirements:\n"
            "1. Start with a single title line beginning with '# '.\n"
            "2. Separate each scene with a line containing only '---'.\n"
            "3. Keep the number of scenes as low as practical for clarity and pacing.\n"
            "4. Each scene should be short enough to fit a 5 to 6 second video clip, ideally about 10 to 16 words.\n"
            "5. Use 1 to 2 compact narration sentences per scene.\n"
            "6. Keep the writing vivid, specific, and suitable for documentary-style visuals.\n"
            "7. Do not add shot lists, markdown bullets, or production notes.\n"
            "8. Return only the script.\n"
        )

        candidate_models = [self.model]
        if self.fallback_model not in candidate_models:
            candidate_models.append(self.fallback_model)

        response = None
        last_error: Exception | None = None

        for model_name in candidate_models:
            try:
                response = self.client.responses.create(
                    model=model_name,
                    input=[
                        {
                            "role": "system",
                            "content": (
                            "You write concise, high-retention YouTube scripts for automated video pipelines. "
                                "Your scripts must be clean, factual in tone, and designed for low-cost short video clips."
                            ),
                        },
                        {
                            "role": "user",
                            "content": prompt,
                        },
                    ],
                )
                break
            except NotFoundError as exc:
                last_error = exc

        if response is None:
            if last_error is not None:
                raise RuntimeError(str(last_error)) from last_error
            raise RuntimeError("OpenAI script generation failed before a response was returned.")

        script = (response.output_text or "").strip()
        if not script:
            raise RuntimeError("OpenAI returned an empty script.")
        return script
