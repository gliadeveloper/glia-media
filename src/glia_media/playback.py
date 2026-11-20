"""짧은 재생 권한을 발급하고 검사한다.

권한은 한 버전의 재생 목록과 그 조각에만 통한다.
원본 MP4와 다른 폴더는 열리지 않는다. 영상 바이트는 여기서 전달하지 않는다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from glia_media.r2_layout import (
    LayoutError,
    playlist_object_key,
    segment_object_key,
)

MINIMUM_GRANT_SECONDS = 10 * 60
GRANT_MARGIN_SECONDS = 30 * 60
MAXIMUM_GRANT_SECONDS = 8 * 60 * 60


class PlaybackError(Exception):
    def __init__(self, summary: str, status: int = 403) -> None:
        self.summary = summary
        self.status = status
        super().__init__(summary)


def issue_grant(
    library: Path,
    asset_id: str,
    version_id: str,
    secret: str,
    now: datetime | None = None,
    playback_base: str = "",
) -> dict[str, object]:
    if not secret:
        raise PlaybackError("재생 권한 서명이 설정되지 않았습니다.", 503)
    manifest, _published_keys = _published(library, asset_id, version_id)
    moment = _clock(now)
    expires = moment + timedelta(seconds=_grant_seconds(manifest))
    payload = {
        "asset_id": asset_id,
        "version_id": version_id,
        "exp": int(expires.timestamp()),
    }
    grant = _sign(payload, secret)
    playlist_key = playlist_object_key(asset_id, version_id)
    issued: dict[str, object] = {
        "grant": grant,
        "expires_at": _iso(expires),
        "playlist_key": playlist_key,
    }
    if playback_base:
        issued["playlist_url"] = playlist_url(playback_base, grant, playlist_key)
    return issued


def playlist_url(playback_base: str, grant: str, playlist_key: str) -> str:
    base = _require_playback_base(playback_base)
    if "/" in grant or not playlist_key.startswith("vod/packages/"):
        raise PlaybackError("재생 주소를 만들지 못했습니다.", 500)
    return f"{base}/p/{grant}/{playlist_key}"


def authorize(
    library: Path,
    grant: str,
    name: str,
    secret: str,
    now: datetime | None = None,
) -> dict[str, object]:
    if not secret:
        raise PlaybackError("재생 권한 서명이 설정되지 않았습니다.", 503)
    payload = _open_grant(grant, secret, _clock(now))
    asset_id = str(payload["asset_id"])
    version_id = str(payload["version_id"])
    published = _require_published(library, asset_id, version_id)
    if name == "index.m3u8":
        key = playlist_object_key(asset_id, version_id)
    else:
        try:
            key = segment_object_key(asset_id, version_id, name)
        except LayoutError as error:
            raise PlaybackError("이 재생 권한으로 열 수 없는 파일입니다.") from error
    if key not in published:
        raise PlaybackError("이 재생 권한으로 열 수 없는 파일입니다.")
    return {"key": key, "expires_at": _iso(datetime.fromtimestamp(int(payload["exp"]), timezone.utc))}


def _grant_seconds(manifest: dict[str, object]) -> int:
    inspection = manifest.get("inspection") if isinstance(manifest.get("inspection"), dict) else {}
    raw = inspection.get("duration_seconds")
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw <= 0:
        return MINIMUM_GRANT_SECONDS
    return min(MAXIMUM_GRANT_SECONDS, max(MINIMUM_GRANT_SECONDS, int(raw) + GRANT_MARGIN_SECONDS))


def _require_published(library: Path, asset_id: str, version_id: str) -> set[str]:
    _manifest, keys = _published(library, asset_id, version_id)
    return keys


def _published(library: Path, asset_id: str, version_id: str) -> tuple[dict[str, object], set[str]]:
    try:
        playlist = playlist_object_key(asset_id, version_id)
    except LayoutError as error:
        raise PlaybackError(error.summary, 400) from error
    version_dir = library / asset_id / version_id
    manifest_path = version_dir / "manifest.json"
    receipt_path = version_dir / "upload.json"
    if not manifest_path.is_file() or not receipt_path.is_file():
        raise PlaybackError("재생할 수 있는 버전이 없습니다.", 404)
    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise PlaybackError("재생 목록 경로가 올바르지 않습니다.", 409)
    manifest: dict[str, object] = loaded
    if not isinstance(manifest.get("inspection"), dict) or not manifest["inspection"].get("passed"):
        raise PlaybackError("검사를 통과하지 않은 버전은 재생하지 않습니다.", 409)
    storage = manifest.get("storage") if isinstance(manifest.get("storage"), dict) else {}
    segment_keys = storage.get("segment_keys")
    if storage.get("playlist_key") != playlist or not isinstance(segment_keys, list):
        raise PlaybackError("재생 목록 경로가 올바르지 않습니다.", 409)
    expected = {playlist, *segment_keys}
    if any(not isinstance(key, str) or not key.startswith("vod/packages/") for key in expected):
        raise PlaybackError("재생 목록 경로가 올바르지 않습니다.", 409)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    present = set(receipt.get("uploaded") or []) | set(receipt.get("already_present") or [])
    if not expected <= present:
        raise PlaybackError("아직 비공개 저장소에 모두 올라가지 않았습니다.", 409)
    return manifest, expected


def _require_playback_base(value: str) -> str:
    parsed = urlparse(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise PlaybackError("재생 주소 설정이 올바르지 않습니다.", 503)
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{parsed.hostname}{port}"


def _sign(payload: dict[str, object], secret: str) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def _open_grant(grant: str, secret: str, now: datetime) -> dict[str, object]:
    body, separator, signature = grant.partition(".")
    if not separator or not body or not signature:
        raise PlaybackError("재생 권한이 올바르지 않습니다.", 401)
    expected = _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    if len(signature) != len(expected) or not hmac.compare_digest(signature, expected):
        raise PlaybackError("재생 권한이 올바르지 않습니다.", 401)
    try:
        payload = json.loads(_b64decode(body))
        exp = int(payload["exp"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise PlaybackError("재생 권한이 올바르지 않습니다.", 401) from error
    if int(now.timestamp()) >= exp:
        raise PlaybackError("재생 권한이 만료되었습니다.", 401)
    return payload


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _clock(now: datetime | None) -> datetime:
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
