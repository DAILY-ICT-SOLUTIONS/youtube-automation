# YouTube Video Automation

Python-based automation for turning a text script into a simple YouTube-ready narrated video using Kie API for media generation and `ffmpeg` for final rendering.

This repo includes both

- a CLI for generating a full video from a script
- a local FastAPI web app for browser-based uploads, generation, and job tracking

## What It Does

- Reads a script from a text file
- Splits the script into scenes
- Generates narration audio with Kie ElevenLabs models
- Downloads a YouTube reference video locally
- Uploads the downloaded reference video to Kie
- Generates one video clip per scene with a low-cost Kie video model
- Builds subtitles from the script timing
- Renders a 1080p MP4 with `ffmpeg`
- Includes a local FastAPI web app for browser-based uploads and job tracking
- Includes a ChatGPT-powered script generator for turning a topic into a YouTube-ready script

## Project Layout

- `src/youtube_automation/` application code
- `examples/sample_script.txt` example input
- `tests/` small parser and subtitle timing tests
- `build/` generated outputs at runtime

## Requirements

- Python 3.11+
- `ffmpeg` installed and available on your `PATH`
- a Kie API key
- an OpenAI API key if you want script generation inside the app

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Then set at least:

- `KIE_API_KEY`
- `OPENAI_API_KEY` if you want topic-to-script generation in the web app

## Script Format

Use a title on the first line with `#`, then separate scenes with `---`.

```text
# The Future of Solar Energy

Solar energy is changing how cities produce power.
Panels are getting cheaper and easier to install.

---

Battery storage makes solar practical even after sunset.
That shift is changing how homes and businesses plan energy use.
```

If you omit `---`, the app will split the script into scene-sized chunks automatically.

## Setup

1. Create a virtual environment.
2. Install the project in editable mode:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

3. Copy `.env.example` to `.env` and set `KIE_API_KEY`.
4. Add `OPENAI_API_KEY` if you want to generate scripts inside the app.
5. Optional: set `KIE_CALLBACK_URL` to a public HTTPS URL if you want Kie to push Veo callbacks to your app.

## Run The CLI

```bash
python3 -m youtube_automation.cli create-video \
  --script-file examples/sample_script.txt \
  --reference-url https://www.youtube.com/watch?v=YOUR_VIDEO_ID \
  --output-dir build/demo
```

## Run In The Browser

Start the local web server:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python3 -m youtube_automation.cli serve-web --reload
```

Then open:

```text
http://127.0.0.1:8000
```

The browser UI lets you:

- generate a script with ChatGPT from a topic and target word count
- paste a script
- paste a YouTube reference video link
- start a render job
- watch job status live
- open the finished MP4 in the browser

## Veo Callbacks

If you want Kie Veo 3.1 to push task results instead of relying only on polling:

- expose your local app with a public HTTPS URL, for example using `ngrok`
- set `KIE_CALLBACK_URL` to:
  - `https://your-public-domain/api/kie/callback`
- restart the app so new Veo tasks include `callBackUrl`

The app will:

- accept Kie callback `POST` requests at `/api/kie/callback`
- log callback payloads to `build/web/jobs/<job_id>/callbacks.jsonl`
- attach callback status information to the matching `*.task.json` file

## Notes

- This starter uses Kie video generation APIs:
  - Default low-cost video generation: `veo3_fast` on Kie's Veo 3.1 API
  - Alternative reference-driven flow: `bytedance/seedance-2`
  - Narration: `elevenlabs/text-to-speech-turbo-2-5`
- `veo3_fast` is the current Veo 3.1 Fast default because it is the lowest-cost verified option I could confirm from Kie's current docs pages.
- The app uses Kie's current Veo API flow:
  - `POST /api/v1/veo/generate`
  - `GET /api/v1/veo/record-info`
  - `GET /api/v1/veo/get-1080p-video`
- If `KIE_CALLBACK_URL` is configured, Veo task creation also sends `callBackUrl` so Kie can push completion events to your app.
- For `veo3_fast`, the app targets Kie's prompt-first Veo 3.1 workflow and fetches the 1080p version of each successful 16:9 clip when available.
- `Seedance 2` uses `reference_video_urls`, while Veo 3.1 uses the dedicated Veo API flow and prompt-first generation.
- The current Kie Veo 3.1 docs mark `enableFallback` as deprecated, so the app no longer sends that field.
- The app downloads the YouTube reference link with `yt-dlp`, then uploads that local file to Kie.
- The scaffold targets `480p` generation from Kie and upscales to the final output size during `ffmpeg` rendering.
- Kie task creation is asynchronous, so the CLI polls for completion before downloading results.
- Subtitles are timed proportionally to the narration duration. This is a practical first version, not word-perfect alignment.
- The local web app keeps job state in memory. If you restart the server, old job status cards will disappear even though generated files remain in `build/web/jobs/`.
- Script generation uses OpenAI's Responses API through the official Python SDK.

## Open Source Notes

- `.env`, `.venv`, `build/`, caches, and package metadata are intentionally excluded from version control
- generated videos and job artifacts are runtime outputs and should not be committed
- add a `LICENSE` file before publishing publicly so reuse terms are clear

## Sources Used

- Kie Getting Started: https://docs.kie.ai/
- Kie Veo 3.1 overview: https://kie.ai/veo-3-1?model=veo%2Fget-1080p-video
- Unified task details endpoint: https://docs.kie.ai/market/common/get-task-detail
- Bytedance Seedance 2.0: https://docs.kie.ai/32356532e0
- Kie file upload quickstart: https://docs.kie.ai/file-upload-api
- ElevenLabs turbo TTS via Kie: https://docs.kie.ai/market/elevenlabs/text-to-speech-turbo-2-5
- OpenAI Responses API: https://platform.openai.com/docs/api-reference/responses
- OpenAI Python SDK setup: https://platform.openai.com/docs/libraries/python-sdk
