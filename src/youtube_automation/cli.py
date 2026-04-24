from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import json
import sys

from .config import Settings, load_dotenv
from .pipeline import VideoPipeline
from .planner import plan_from_file


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Create narrated YouTube videos from a script using Kie API.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_video = subparsers.add_parser("create-video", help="Generate a full video.")
    create_video.add_argument("--script-file", type=Path, required=True, help="Path to the input script text file.")
    create_video.add_argument("--reference-url", required=True, help="YouTube reference video URL used as Seedance video input.")
    create_video.add_argument("--output-dir", type=Path, required=True, help="Directory for generated assets and video.")

    show_plan = subparsers.add_parser("plan", help="Preview scene breakdown without calling Kie.")
    show_plan.add_argument("--script-file", type=Path, required=True, help="Path to the input script text file.")

    serve_web = subparsers.add_parser("serve-web", help="Run the local FastAPI web app.")
    serve_web.add_argument("--host", default="127.0.0.1", help="Host to bind the local server to.")
    serve_web.add_argument("--port", type=int, default=8000, help="Port for the local server.")
    serve_web.add_argument("--reload", action="store_true", help="Enable auto-reload during development.")

    return parser


def cmd_plan(script_file: Path) -> int:
    plan = plan_from_file(script_file)
    payload = {
        "title": plan.title,
        "scene_count": len(plan.scenes),
        "scenes": [
            {
                "index": scene.index,
                "narration": scene.narration,
                "visual_prompt": scene.visual_prompt,
            }
            for scene in plan.scenes
        ],
    }
    print(json.dumps(payload, indent=2))
    return 0


def cmd_create_video(script_file: Path, reference_url: str, output_dir: Path) -> int:
    settings = Settings.from_env()
    plan = plan_from_file(script_file)
    pipeline = VideoPipeline(settings)
    video_path = pipeline.create_video(plan, reference_url, output_dir)
    print(f"Created video: {video_path}")
    return 0


def cmd_serve_web(host: str, port: int, reload: bool) -> int:
    import uvicorn

    uvicorn.run(
        "youtube_automation.web:app",
        host=host,
        port=port,
        reload=reload,
    )
    return 0


def main() -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "plan":
            return cmd_plan(args.script_file)
        if args.command == "create-video":
            return cmd_create_video(args.script_file, args.reference_url, args.output_dir)
        if args.command == "serve-web":
            return cmd_serve_web(args.host, args.port, args.reload)
        parser.error(f"Unknown command: {args.command}")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
