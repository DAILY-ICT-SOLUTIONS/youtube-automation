from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus
import json
import subprocess
import time


class KieAPIError(RuntimeError):
    pass


class KiePendingError(KieAPIError):
    pass


@dataclass(slots=True)
class KieTask:
    task_id: str
    state: str | None = None
    result_json: str | None = None
    fail_msg: str | None = None


class KieClient:
    def __init__(self, api_key: str, base_url: str, upload_base_url: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.upload_base_url = upload_base_url.rstrip("/")

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "youtube-automation/0.1",
        }

    @staticmethod
    def _is_transient_status_error(message: str) -> bool:
        lowered = message.lower()
        return (
            "please try again later" in lowered
            or "internal error" in lowered
            or "is being generated" in lowered
        )

    def _request_json(self, method: str, path: str, payload: dict | None = None) -> dict:
        command = [
            "curl",
            "--connect-timeout",
            "20",
            "--max-time",
            "180",
            "--retry",
            "3",
            "--retry-all-errors",
            "-sS",
            "-X",
            method,
            f"{self.base_url}{path}",
            "-H",
            f"Authorization: Bearer {self.api_key}",
            "-H",
            "Content-Type: application/json",
            "-H",
            "User-Agent: youtube-automation/0.1",
        ]
        if payload is not None:
            command.extend(["-d", json.dumps(payload)])

        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip() or "curl exited without an error message."
            raise KieAPIError(f"Kie request failed: {details}")

        body = completed.stdout

        data = json.loads(body)
        if data.get("code") != 200:
            message = data.get("msg", "Unknown Kie API error")
            lowered = message.lower()
            if "please try again later" in lowered or "is being generated" in lowered:
                raise KiePendingError(message)
            raise KieAPIError(message)
        return data

    def create_task(self, model: str, input_payload: dict, callback_url: str | None = None) -> str:
        payload = {"model": model, "input": input_payload}
        if callback_url:
            payload["callBackUrl"] = callback_url

        data = self._request_json("POST", "/api/v1/jobs/createTask", payload)
        task_id = data.get("data", {}).get("taskId")
        if not task_id:
            raise KieAPIError("Kie API did not return a taskId.")
        return task_id

    def create_veo_task(
        self,
        *,
        prompt: str,
        model: str,
        aspect_ratio: str = "16:9",
        resolution: str | None = None,
        callback_url: str | None = None,
        image_urls: list[str] | None = None,
        generation_type: str | None = None,
    ) -> str:
        payload: dict[str, object] = {
            "prompt": prompt,
            "model": model,
            "aspectRatio": aspect_ratio,
            "enableFallback": False,
            "enableTranslation": True,
        }
        if resolution:
            payload["resolution"] = resolution
        if callback_url:
            payload["callBackUrl"] = callback_url
        if image_urls:
            payload["imageUrls"] = image_urls
        if generation_type:
            payload["generationType"] = generation_type

        data = self._request_json("POST", "/api/v1/veo/generate", payload)
        task_id = data.get("data", {}).get("taskId")
        if not task_id:
            raise KieAPIError("Kie Veo API did not return a taskId.")
        return task_id

    def get_veo_task(self, task_id: str) -> KieTask:
        safe_task_id = quote_plus(task_id)
        data = self._request_json("GET", f"/api/v1/veo/record-info?taskId={safe_task_id}")
        payload = data.get("data", {})
        response_payload = payload.get("response") or {}
        result_urls = (
            response_payload.get("fullResultUrls")
            or response_payload.get("full_result_urls")
            or response_payload.get("resultUrls")
            or payload.get("resultUrls")
        )
        if isinstance(result_urls, str):
            try:
                result_urls = json.loads(result_urls)
            except json.JSONDecodeError:
                result_urls = [result_urls]
        result_json = json.dumps({"resultUrls": result_urls})
        success_flag = payload.get("successFlag")
        state = "success" if success_flag == 1 else "fail" if success_flag in {2, 3} else "processing"
        return KieTask(
            task_id=payload.get("taskId", task_id),
            state=state,
            result_json=result_json,
            fail_msg=payload.get("errorMessage") or payload.get("msg"),
        )

    def get_veo_1080p_video_url(self, task_id: str, index: int = 0) -> str | None:
        safe_task_id = quote_plus(task_id)
        data = self._request_json("GET", f"/api/v1/veo/get-1080p-video?taskId={safe_task_id}&index={index}")
        return data.get("data", {}).get("resultUrl")

    def get_task(self, task_id: str) -> KieTask:
        safe_task_id = quote_plus(task_id)
        data = self._request_json("GET", f"/api/v1/jobs/recordInfo?taskId={safe_task_id}")
        payload = data.get("data", {})
        return KieTask(
            task_id=payload.get("taskId", task_id),
            state=payload.get("state"),
            result_json=payload.get("resultJson"),
            fail_msg=payload.get("failMsg"),
        )

    def wait_for_task(
        self,
        task_id: str,
        *,
        timeout_seconds: int = 900,
        poll_seconds: int = 5,
        task_kind: str = "market",
    ) -> dict:
        started = time.time()

        while True:
            try:
                task = self.get_veo_task(task_id) if task_kind == "veo" else self.get_task(task_id)
            except KiePendingError:
                task = None
            except KieAPIError as exc:
                if self._is_transient_status_error(str(exc)):
                    task = None
                else:
                    raise

            if task is None:
                if (time.time() - started) > timeout_seconds:
                    raise TimeoutError(f"Timed out waiting for Kie task {task_id}.")
                time.sleep(poll_seconds)
                continue

            state = (task.state or "").lower()

            if state == "success":
                return json.loads(task.result_json or "{}")

            if state == "fail":
                if self._is_transient_status_error(task.fail_msg or ""):
                    if (time.time() - started) > timeout_seconds:
                        raise TimeoutError(f"Timed out waiting for Kie task {task_id}.")
                    time.sleep(poll_seconds)
                    continue
                raise KieAPIError(task.fail_msg or f"Kie task {task_id} failed.")

            if (time.time() - started) > timeout_seconds:
                raise TimeoutError(f"Timed out waiting for Kie task {task_id}.")

            time.sleep(poll_seconds)

    def try_get_veo_1080p_video_url(self, task_id: str, *, index: int = 0) -> str | None:
        try:
            return self.get_veo_1080p_video_url(task_id, index=index)
        except KiePendingError:
            return None

    def download_file(self, url: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                "curl",
                "--connect-timeout",
                "20",
                "--max-time",
                "300",
                "--retry",
                "3",
                "--retry-all-errors",
                "-sS",
                "-L",
                url,
                "-o",
                str(destination),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip() or "curl exited without an error message."
            raise KieAPIError(f"Download failed: {details}")

        return destination

    def upload_file(self, source: Path, upload_path: str = "video-inputs") -> str:
        completed = subprocess.run(
            [
                "curl",
                "--connect-timeout",
                "20",
                "--max-time",
                "300",
                "-sS",
                "-X",
                "POST",
                f"{self.upload_base_url}/api/file-stream-upload",
                "-H",
                f"Authorization: Bearer {self.api_key}",
                "-F",
                f"file=@{source}",
                "-F",
                f"uploadPath={upload_path}",
                "-F",
                f"fileName={source.name}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            stdout = completed.stdout.strip()
            details = stderr or stdout or "curl exited without an error message."
            raise KieAPIError(f"Kie upload failed: {details}")

        body = completed.stdout

        data = json.loads(body)
        if data.get("code") != 200:
            raise KieAPIError(data.get("msg", "Unknown Kie upload error"))

        file_url = data.get("data", {}).get("fileUrl") or data.get("data", {}).get("downloadUrl")
        if not file_url:
            raise KieAPIError("Kie upload did not return a file URL.")
        return file_url


def find_urls(payload: object) -> list[str]:
    urls: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, str) and node.startswith(("http://", "https://")):
            urls.append(node)

    walk(payload)
    return urls
