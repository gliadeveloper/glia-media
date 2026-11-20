"""로컬 버전을 HLS 전용 버킷의 vod/ 아래로만 올린다.

이미 있는 객체는 크기가 같으면 건너뛰고, 다르면 덮어쓰지 않는다.
삭제하지 않는다.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from glia_media.r2_config import R2Config
from glia_media.r2_layout import (
    LayoutError,
    list_segment_names,
    playlist_object_key,
    segment_object_key,
    source_object_key,
)

SINGLE_PUT_MAX = 64 * 1024 * 1024
PART_SIZE = 8 * 1024 * 1024
_UPLOAD_WORKERS = 4
_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
}


class UploadError(Exception):
    def __init__(self, summary: str) -> None:
        self.summary = summary
        super().__init__(summary)


class PreconditionFailed(Exception):
    pass


class PlannedObject:
    def __init__(self, key: str, path: Path, content_type: str, size: int) -> None:
        self.key = key
        self.path = path
        self.content_type = content_type
        self.size = size


class UploadResult:
    def __init__(self, bucket: str, uploaded: list[str], already_present: list[str]) -> None:
        self.bucket = bucket
        self.uploaded = uploaded
        self.already_present = already_present


class ObjectStore:
    def head_size(self, key: str) -> int | None:
        raise NotImplementedError

    def put_new(self, item: PlannedObject) -> None:
        raise NotImplementedError


def plan_version_upload(library: Path, asset_id: str, version_id: str) -> list[PlannedObject]:
    version_dir = library / asset_id / version_id
    manifest_path = version_dir / "manifest.json"
    if not manifest_path.is_file():
        raise UploadError("올릴 버전 기록이 없습니다.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("asset_id") != asset_id or manifest.get("version_id") != version_id:
        raise UploadError("버전 기록의 번호가 폴더와 다릅니다.")
    if not manifest.get("inspection", {}).get("passed"):
        raise UploadError("검사를 통과하지 않은 버전은 올리지 않습니다.")

    package_dir = version_dir / "720p"
    try:
        segment_names = list_segment_names(package_dir)
    except LayoutError as error:
        raise UploadError(error.summary) from None
    if not segment_names:
        raise UploadError("올릴 영상 조각이 없습니다.")
    try:
        expected = {
            "source_key": source_object_key(asset_id, version_id),
            "playlist_key": playlist_object_key(asset_id, version_id),
            "segment_keys": [
                segment_object_key(asset_id, version_id, name) for name in segment_names
            ],
        }
    except LayoutError as error:
        raise UploadError(error.summary) from None
    if manifest.get("storage") != expected:
        raise UploadError("버전 기록의 저장 경로가 vod/ 설계와 다릅니다.")

    objects = [
        _planned(expected["segment_keys"][index], package_dir / name)
        for index, name in enumerate(segment_names)
    ]
    objects.append(_planned(expected["source_key"], version_dir / "source.mp4"))
    objects.append(_planned(expected["playlist_key"], package_dir / "index.m3u8"))
    return objects


def upload_planned(objects: list[PlannedObject], store: ObjectStore, bucket: str) -> UploadResult:
    segments = [item for item in objects if item.key.endswith(".ts")]
    remainder = [item for item in objects if not item.key.endswith(".ts")]
    uploaded: list[str] = []
    already_present: list[str] = []
    for kind, key in _upload_segments(segments, store) + [_upload_one(item, store) for item in remainder]:
        if kind == "uploaded":
            uploaded.append(key)
        else:
            already_present.append(key)
    return UploadResult(bucket, uploaded, already_present)


def _upload_segments(items: list[PlannedObject], store: ObjectStore) -> list[tuple[str, str]]:
    if len(items) <= 1:
        return [_upload_one(item, store) for item in items]
    workers = min(_UPLOAD_WORKERS, len(items))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda item: _upload_one(item, store), items))


def _upload_one(item: PlannedObject, store: ObjectStore) -> tuple[str, str]:
    if not item.key.startswith("vod/"):
        raise UploadError("vod/ 밖의 경로는 올리지 않습니다.")
    remote_size = store.head_size(item.key)
    if remote_size is None:
        try:
            store.put_new(item)
        except PreconditionFailed:
            remote_size = store.head_size(item.key)
            _require_same_size(item, remote_size)
            return "present", item.key
        return "uploaded", item.key
    _require_same_size(item, remote_size)
    return "present", item.key


def upload_version(
    library: Path,
    asset_id: str,
    version_id: str | None,
    config: R2Config,
    store: ObjectStore | None = None,
) -> UploadResult:
    chosen = version_id or _current_version_id(library, asset_id)
    objects = plan_version_upload(library, asset_id, chosen)
    active_store = store if store is not None else BotoObjectStore(config)
    result = upload_planned(objects, active_store, config.bucket)
    receipt = {
        "bucket": result.bucket,
        "uploaded": result.uploaded,
        "already_present": result.already_present,
    }
    (library / asset_id / chosen / "upload.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def _current_version_id(library: Path, asset_id: str) -> str:
    path = library / asset_id / "asset.json"
    if not path.is_file():
        raise UploadError("영상 기록이 없습니다.")
    asset = json.loads(path.read_text(encoding="utf-8"))
    version_id = asset.get("current_version_id")
    if not isinstance(version_id, str) or not version_id:
        raise UploadError("현재 버전 번호가 없습니다.")
    return version_id


def _planned(key: str, path: Path) -> PlannedObject:
    if not path.is_file():
        raise UploadError(f"로컬 파일이 없습니다: {path.name}")
    return PlannedObject(key, path, _CONTENT_TYPES[path.suffix], path.stat().st_size)


def _require_same_size(item: PlannedObject, remote_size: int | None) -> None:
    if remote_size != item.size:
        raise UploadError(f"이미 다른 파일이 있어 덮어쓰지 않았습니다: {item.key}")


class BotoObjectStore(ObjectStore):
    def __init__(self, config: R2Config) -> None:
        import boto3
        from botocore.config import Config

        self._bucket = config.bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=config.endpoint,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            region_name=config.region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def head_size(self, key: str) -> int | None:
        from botocore.exceptions import ClientError

        try:
            response = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            if _status(error) == 404 or _code(error) in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise UploadError("저장소에서 파일 정보를 읽지 못했습니다.") from error
        return int(response["ContentLength"])

    def put_new(self, item: PlannedObject) -> None:
        from botocore.exceptions import ClientError

        try:
            if item.size <= SINGLE_PUT_MAX:
                self._put_single(item)
            else:
                self._put_multipart(item)
        except ClientError as error:
            if _status(error) == 412 or _code(error) in {"PreconditionFailed", "412"}:
                raise PreconditionFailed from error
            raise UploadError("저장소에 파일을 쓰지 못했습니다.") from error

    def _put_single(self, item: PlannedObject) -> None:
        with item.path.open("rb") as body:
            self._client.put_object(
                Bucket=self._bucket,
                Key=item.key,
                Body=body,
                ContentLength=item.size,
                ContentType=item.content_type,
                IfNoneMatch="*",
            )

    def _put_multipart(self, item: PlannedObject) -> None:
        created = self._client.create_multipart_upload(
            Bucket=self._bucket,
            Key=item.key,
            ContentType=item.content_type,
        )
        upload_id = created["UploadId"]
        parts: list[dict[str, object]] = []
        try:
            with item.path.open("rb") as body:
                part_number = 1
                while True:
                    chunk = body.read(PART_SIZE)
                    if not chunk:
                        break
                    response = self._client.upload_part(
                        Bucket=self._bucket,
                        Key=item.key,
                        UploadId=upload_id,
                        PartNumber=part_number,
                        Body=chunk,
                        ContentLength=len(chunk),
                    )
                    parts.append({"PartNumber": part_number, "ETag": response["ETag"]})
                    part_number += 1
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=item.key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception:
            self._client.abort_multipart_upload(
                Bucket=self._bucket,
                Key=item.key,
                UploadId=upload_id,
            )
            raise


def _status(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return None
    metadata = response.get("ResponseMetadata", {})
    status = metadata.get("HTTPStatusCode")
    return status if isinstance(status, int) else None


def _code(error: Exception) -> str:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return ""
    code = response.get("Error", {}).get("Code", "")
    return code if isinstance(code, str) else ""
