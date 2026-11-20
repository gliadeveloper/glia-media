"""LMS와 주고받는 내부 API.

LMS 데이터베이스는 열지 않는다. LMS가 변환을 요청하고,
끝나면 영상 번호와 상태만 돌려준다.
"""

from __future__ import annotations

import hmac
import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from glia_media.jobs import Job, JobError, load_job, request_job
from glia_media.r2_layout import LayoutError, playlist_object_key

_MAX_BODY = 64 * 1024
Sender = Callable[[str, str, bytes], None]


class LmsError(Exception):
    def __init__(self, summary: str, status: int = 400) -> None:
        self.summary = summary
        self.status = status
        super().__init__(summary)


def job_view(library: Path, job: Job) -> dict[str, object]:
    duration = None
    playlist_key = None
    if job.asset_id and job.version_id:
        manifest_path = library / job.asset_id / job.version_id / "manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            value = manifest.get("inspection", {}).get("duration_seconds")
            if isinstance(value, (int, float)):
                duration = float(value)
        try:
            playlist_key = playlist_object_key(job.asset_id, job.version_id)
        except LayoutError:
            playlist_key = None
    state = job.state if job.state in {"READY", "FAILED"} else "PROCESSING"
    return {
        "job_id": job.id,
        "lms_request_id": job.lms_request_id,
        "state": state,
        "asset_id": job.asset_id,
        "version_id": job.version_id,
        "duration_seconds": duration,
        "playlist_key": playlist_key,
        "error": job.last_error,
    }


def handle_request(
    library: Path,
    method: str,
    path: str,
    authorization: str,
    body: bytes,
    token: str,
) -> tuple[int, dict[str, object]]:
    if not _authorized(authorization, token):
        raise LmsError("인증이 필요합니다.", 401)
    if method == "POST" and path == "/v1/jobs":
        return _create_job(library, body)
    if method == "GET" and path.startswith("/v1/jobs/"):
        job_id = path.removeprefix("/v1/jobs/")
        if "/" in job_id or not job_id:
            raise LmsError("작업 주소를 확인해 주세요.", 404)
        try:
            job = load_job(library, job_id)
        except JobError as error:
            raise LmsError(error.summary, 404) from None
        return 200, job_view(library, job)
    raise LmsError("요청 주소를 확인해 주세요.", 404)


def lms_settings(
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = None,
) -> dict[str, str]:
    values = dict(os.environ if environ is None else environ)
    if env_file is not None and env_file.is_file():
        for key, value in _parse_env_file(env_file).items():
            values.setdefault(key, value)
    return {
        "token": values.get("GLIA_LMS_TOKEN", "").strip(),
        "callback_url": values.get("GLIA_LMS_CALLBACK_URL", "").strip(),
        "listen": values.get("GLIA_LISTEN", "").strip() or "127.0.0.1:8765",
    }


def listen_address(value: str) -> tuple[str, int]:
    host, separator, port_text = value.rpartition(":")
    if not separator:
        raise LmsError("GLIA_LISTEN 은 호스트:포트 형식입니다.")
    try:
        port = int(port_text)
    except ValueError as error:
        raise LmsError("GLIA_LISTEN 은 호스트:포트 형식입니다.") from error
    if not host or port < 1 or port > 65535:
        raise LmsError("GLIA_LISTEN 은 호스트:포트 형식입니다.")
    return host, port


def notify_lms(
    url: str,
    token: str,
    payload: dict[str, object],
    send: Sender | None = None,
    attempts: int = 3,
) -> None:
    if payload.get("state") not in {"READY", "FAILED"}:
        return
    _require_callback_url(url)
    sender = send if send is not None else _post_json
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_status = None
    for _ in range(attempts):
        try:
            sender(url, token, body)
            return
        except LmsError as error:
            last_status = error.status
        except (OSError, urllib.error.URLError):
            last_status = None
    raise LmsError("LMS에 완료 알림을 보내지 못했습니다.", last_status or 502)


def _create_job(library: Path, body: bytes) -> tuple[int, dict[str, object]]:
    if len(body) > _MAX_BODY:
        raise LmsError("요청이 너무 큽니다.", 413)
    try:
        incoming = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LmsError("요청 본문을 읽을 수 없습니다.") from error
    if not isinstance(incoming, dict):
        raise LmsError("요청 본문을 읽을 수 없습니다.")
    source = incoming.get("source_path")
    asset_id = incoming.get("asset_id")
    lms_request_id = incoming.get("lms_request_id")
    if not isinstance(source, str) or not source:
        raise LmsError("원본 MP4 경로가 필요합니다.")
    if asset_id is not None and not isinstance(asset_id, str):
        raise LmsError("영상 번호 형식이 올바르지 않습니다.")
    if lms_request_id is not None and not isinstance(lms_request_id, str):
        raise LmsError("LMS 요청 번호 형식이 올바르지 않습니다.")
    try:
        job = request_job(library, Path(source), asset_id, lms_request_id)
    except JobError as error:
        raise LmsError(error.summary) from None
    status = 200 if job.state in {"READY", "FAILED"} else 202
    return status, job_view(library, job)


def _authorized(authorization: str, token: str) -> bool:
    if not token or not authorization.startswith("Bearer "):
        return False
    given = authorization.removeprefix("Bearer ")
    if len(given) != len(token):
        return False
    return hmac.compare_digest(given, token)


def _require_callback_url(url: str) -> None:
    parsed = urlparse(url)
    local = parsed.hostname in {"127.0.0.1", "localhost"}
    if parsed.scheme == "https" or (parsed.scheme == "http" and local):
        if not parsed.username and not parsed.password:
            return
    raise LmsError("LMS 알림 주소는 https 이거나 이 컴퓨터의 주소여야 합니다.")


def _parse_env_file(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.startswith("GLIA_LMS_") and key != "GLIA_LISTEN":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        parsed[key] = value
    return parsed


def _post_json(url: str, token: str, body: bytes) -> None:
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status >= 300:
                raise LmsError("LMS가 완료 알림을 받지 않았습니다.", response.status)
    except urllib.error.HTTPError as error:
        raise LmsError("LMS가 완료 알림을 받지 않았습니다.", error.code) from None
