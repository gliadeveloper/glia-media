"""변환 결과를 덮어쓰지 않는 영상 버전으로 보관한다.

데이터베이스는 아직 쓰지 않는다. 라이브러리 폴더가 기록부다.
새 버전을 만들어도 이전 버전 파일은 그대로 두고, 현재 가리키는 버전만 바꾼다.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from glia_media.convert import ENCODE_SETTINGS, ConvertError, convert
from glia_media.r2_layout import LayoutError, playlist_object_key, segment_object_key, source_object_key

_ID = re.compile(r"^[0-9a-f]{32}$")


class VersionError(Exception):
    def __init__(self, summary: str) -> None:
        self.summary = summary
        super().__init__(summary)


@dataclass(frozen=True)
class VersionRecord:
    asset_id: str
    version_id: str
    number: int
    playlist: Path
    width: int
    height: int
    duration_seconds: float
    segment_count: int


def publish_version(source: Path, library: Path, asset_id: str | None = None) -> VersionRecord:
    source = source.expanduser().resolve()
    library = library.expanduser().resolve()
    if not source.is_file():
        raise VersionError(f"원본 파일을 찾을 수 없습니다: {source}")
    if source.suffix.lower() != ".mp4":
        raise VersionError(f"원본은 MP4 파일이어야 합니다: {source}")
    library.mkdir(parents=True, exist_ok=True)
    asset_id_was_given = asset_id is not None
    if asset_id is None:
        asset_id = uuid.uuid4().hex
    else:
        _require_id(asset_id, "영상 번호")
    version_id = uuid.uuid4().hex
    with _lock(library, asset_id):
        return _publish_locked(
            source,
            library,
            asset_id,
            version_id,
            asset_id_was_given,
        )


def use_version(library: Path, asset_id: str, version_id: str) -> VersionRecord:
    library = library.expanduser().resolve()
    _require_id(asset_id, "영상 번호")
    _require_id(version_id, "버전 번호")
    with _lock(library, asset_id):
        asset = _read_asset(library, asset_id)
        known = {item["version_id"] for item in asset["versions"]}
        if version_id not in known:
            raise VersionError("이 영상에 없는 버전입니다. 현재 버전은 바꾸지 않았습니다.")
        manifest = _read_manifest(library, asset_id, version_id)
        version_dir = _version_dir(library, asset_id, version_id)
        playlist = version_dir / "720p" / "index.m3u8"
        stored_source = version_dir / "source.mp4"
        if not playlist.is_file() or not stored_source.is_file():
            raise VersionError("버전 파일이 없습니다. 현재 버전은 바꾸지 않았습니다.")
        if not manifest.get("inspection", {}).get("passed"):
            raise VersionError("검사에 통과한 버전이 아닙니다. 현재 버전은 바꾸지 않았습니다.")
        asset["current_version_id"] = version_id
        _write_json(_asset_path(library, asset_id), asset)
        return _record_from_manifest(manifest, playlist)


def _publish_locked(
    source: Path,
    library: Path,
    asset_id: str,
    version_id: str,
    asset_id_was_given: bool,
) -> VersionRecord:
    asset_path = _asset_path(library, asset_id)
    if asset_path.is_file():
        asset = _read_asset(library, asset_id)
        number = max(item["number"] for item in asset["versions"]) + 1
    elif asset_id_was_given:
        raise VersionError(f"영상을 찾을 수 없습니다: {asset_id}")
    else:
        asset = None
        number = 1

    incoming = library / ".incoming" / version_id
    if incoming.exists():
        raise VersionError("같은 버전 작업이 이미 있습니다.")
    incoming.mkdir(parents=True)
    published = False
    try:
        stored_source = incoming / "source.mp4"
        before = _sha256(source)
        shutil.copyfile(source, stored_source)
        if _sha256(stored_source) != before or _sha256(source) != before:
            raise VersionError("원본을 버전 폴더로 복사하는 중 내용이 달라졌습니다.")
        result = convert(stored_source, incoming / "720p")
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        manifest = {
            "asset_id": asset_id,
            "version_id": version_id,
            "number": number,
            "created_at": created_at,
            "source": {
                "file": "source.mp4",
                "original_name": source.name,
                "sha256": before,
                "byte_size": stored_source.stat().st_size,
            },
            "settings": ENCODE_SETTINGS,
            "inspection": {
                "passed": True,
                "width": result.width,
                "height": result.height,
                "duration_seconds": result.duration_seconds,
                "segment_count": result.segment_count,
                "video_codec": ENCODE_SETTINGS["video_codec"],
                "audio_codec": ENCODE_SETTINGS["audio_codec"],
                "checks": [
                    "playlist",
                    "segments",
                    "720p",
                    "codecs",
                    "duration",
                    "decode",
                    "source_unchanged",
                ],
            },
            "playback": {"playlist": "720p/index.m3u8"},
            "storage": _storage_record(asset_id, version_id, incoming / "720p"),
        }
        _write_json(incoming / "manifest.json", manifest)
        destination = _version_dir(library, asset_id, version_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.rename(incoming, destination)
        published = True
        versions = [] if asset is None else list(asset["versions"])
        versions.append(
            {"version_id": version_id, "number": number, "created_at": created_at}
        )
        _write_json(
            asset_path,
            {
                "asset_id": asset_id,
                "current_version_id": version_id,
                "versions": versions,
            },
        )
        return _record_from_manifest(manifest, destination / "720p" / "index.m3u8")
    except LayoutError as error:
        if not published:
            shutil.rmtree(incoming, ignore_errors=True)
        raise VersionError(error.summary) from None
    except ConvertError as error:
        if not published:
            shutil.rmtree(incoming, ignore_errors=True)
        raise ConvertError(error.summary, error.detail) from None
    except (VersionError, OSError):
        if not published:
            shutil.rmtree(incoming, ignore_errors=True)
        raise


def _storage_record(asset_id: str, version_id: str, package_dir: Path) -> dict:
    segment_names = sorted(
        path.name for path in package_dir.glob("segment-*.ts") if path.is_file()
    )
    return {
        "source_key": source_object_key(asset_id, version_id),
        "playlist_key": playlist_object_key(asset_id, version_id),
        "segment_keys": [
            segment_object_key(asset_id, version_id, name) for name in segment_names
        ],
    }


def _record_from_manifest(manifest: dict, playlist: Path) -> VersionRecord:
    inspection = manifest["inspection"]
    return VersionRecord(
        asset_id=manifest["asset_id"],
        version_id=manifest["version_id"],
        number=int(manifest["number"]),
        playlist=playlist,
        width=int(inspection["width"]),
        height=int(inspection["height"]),
        duration_seconds=float(inspection["duration_seconds"]),
        segment_count=int(inspection["segment_count"]),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_id(value: str, label: str) -> None:
    if _ID.fullmatch(value) is None:
        raise VersionError(f"{label} 형식이 올바르지 않습니다.")


def _asset_path(library: Path, asset_id: str) -> Path:
    return library / asset_id / "asset.json"


def _version_dir(library: Path, asset_id: str, version_id: str) -> Path:
    return library / asset_id / version_id


def _read_asset(library: Path, asset_id: str) -> dict:
    path = _asset_path(library, asset_id)
    if not path.is_file():
        raise VersionError(f"영상을 찾을 수 없습니다: {asset_id}")
    return _read_json(path, "영상 기록")


def _read_manifest(library: Path, asset_id: str, version_id: str) -> dict:
    path = _version_dir(library, asset_id, version_id) / "manifest.json"
    if not path.is_file():
        raise VersionError("버전 기록이 없습니다. 현재 버전은 바꾸지 않았습니다.")
    return _read_json(path, "버전 기록")


def _read_json(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise VersionError(f"{label}을 읽지 못했습니다.") from error
    if not isinstance(data, dict):
        raise VersionError(f"{label}을 읽지 못했습니다.")
    return data


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


@contextmanager
def _lock(library: Path, asset_id: str):
    lock_dir = library / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{asset_id}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
