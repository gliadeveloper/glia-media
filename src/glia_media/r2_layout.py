"""HLS 객체 키를 운영 중인 LMS 폴더 밖에 둔다.

online.glia가 이미 쓰는 경로는 아래에 있다.

- 보관함: media/shared, media/personal, media/staging
- 기존 파일: avatars, community, lesson-qna, courses, coaching, products
- 그 기존 파일의 물리 위치: online-glia/ 를 앞에 붙인 키

이 모듈은 vod/ 만 만든다. 네트워크로 올리지 않는다.
"""

from __future__ import annotations

import re

_ID = re.compile(r"^[0-9a-f]{32}$")
_SEGMENT = re.compile(r"^segment-\d{3}\.ts$")
_RESERVED = (
    "avatars/",
    "community/",
    "lesson-qna/",
    "courses/",
    "coaching/",
    "products/",
    "media/",
    "online-glia/",
    "staging/",
)


class LayoutError(Exception):
    def __init__(self, summary: str) -> None:
        self.summary = summary
        super().__init__(summary)


def source_object_key(asset_id: str, version_id: str) -> str:
    return _key(f"vod/originals/{_require_id(asset_id)}/{_require_id(version_id)}/source.mp4")


def playlist_object_key(asset_id: str, version_id: str) -> str:
    return _key(f"vod/packages/{_require_id(asset_id)}/{_require_id(version_id)}/720p/index.m3u8")


def segment_object_key(asset_id: str, version_id: str, segment_name: str) -> str:
    if _SEGMENT.fullmatch(segment_name) is None:
        raise LayoutError(f"조각 파일 이름이 올바르지 않습니다: {segment_name}")
    prefix = f"vod/packages/{_require_id(asset_id)}/{_require_id(version_id)}/720p"
    return _key(f"{prefix}/{segment_name}")


def _require_id(value: str) -> str:
    if _ID.fullmatch(value) is None:
        raise LayoutError("영상 번호 형식이 올바르지 않습니다.")
    return value


def _key(key: str) -> str:
    if (
        not key
        or key.startswith("/")
        or "\\" in key
        or any(part in {"", ".", ".."} for part in key.split("/"))
        or key.startswith(_RESERVED)
    ):
        raise LayoutError("저장 경로가 기존 폴더와 겹치거나 올바르지 않습니다.")
    return key
