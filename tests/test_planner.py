from pathlib import Path
import unittest

from youtube_automation.ffmpeg import _format_srt_time, build_scene_assets
from youtube_automation.planner import MAX_WORDS_PER_SCENE
from youtube_automation.planner import plan_from_script


class PlannerTests(unittest.TestCase):
    def test_plan_from_script_with_scene_markers(self) -> None:
        plan = plan_from_script(
            "# Demo Title\n\n"
            "First scene sentence one. First scene sentence two.\n\n"
            "---\n\n"
            "Second scene sentence."
        )

        self.assertEqual(plan.title, "Demo Title")
        self.assertEqual(len(plan.scenes), 2)
        self.assertIn("First scene sentence one.", plan.scenes[0].narration)

    def test_plan_from_script_auto_chunks_single_block(self) -> None:
        plan = plan_from_script(
            "# Demo\n\n"
            "One. Two. Three. Four."
        )

        self.assertEqual(len(plan.scenes), 1)

    def test_plan_rebalances_long_scene_blocks_for_short_clips(self) -> None:
        plan = plan_from_script(
            "# Demo\n\n"
            "This is a much longer scene block designed to exceed the short clip budget and force the planner "
            "to break the narration into smaller, cheaper, scene-sized chunks for generation."
        )

        self.assertGreater(len(plan.scenes), 1)
        self.assertTrue(all(len(scene.narration.split()) <= MAX_WORDS_PER_SCENE for scene in plan.scenes))

    def test_build_scene_assets_covers_full_duration(self) -> None:
        assets = build_scene_assets(
            scene_image_paths=[Path("a.png"), Path("b.png")],
            narrations=["short text", "this one is much longer than the first one"],
            audio_duration=10.0,
        )

        self.assertEqual(assets[0].start_time, 0.0)
        self.assertEqual(round(assets[-1].end_time, 3), 10.0)

    def test_format_srt_time(self) -> None:
        self.assertEqual(_format_srt_time(65.432), "00:01:05,432")


if __name__ == "__main__":
    unittest.main()
