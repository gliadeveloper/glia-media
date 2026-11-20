"""HLS 전용 R2 설정.

장기 키는 버킷 단위로만 제한된다. 그래서 LMS 버킷 online-glia 는 받지 않는다.
키 값은 예외 문구에 넣지 않는다.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

LMS_BUCKET = "online-glia"
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


class ConfigError(Exception):
    def __init__(self, summary: str) -> None:
        self.summary = summary
        super().__init__(summary)


class R2Config:
    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str,
    ) -> None:
        self.endpoint = endpoint
        self.bucket = bucket
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.region = region


def load_r2_config(
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = None,
) -> R2Config:
    values = dict(os.environ if environ is None else environ)
    if env_file is not None and env_file.is_file():
        for key, value in _parse_env_file(env_file).items():
            values.setdefault(key, value)

    endpoint = values.get("GLIA_R2_ENDPOINT", "").strip()
    bucket = values.get("GLIA_R2_BUCKET", "").strip()
    access_key_id = values.get("GLIA_R2_ACCESS_KEY_ID", "").strip()
    secret_access_key = values.get("GLIA_R2_SECRET_ACCESS_KEY", "").strip()
    region = values.get("GLIA_R2_REGION", "").strip() or "auto"
    public_base_url = values.get("GLIA_R2_PUBLIC_BASE_URL", "").strip()

    if not endpoint or not bucket or not access_key_id or not secret_access_key:
        raise ConfigError(
            "HLS 전용 버킷 설정이 없습니다. "
            "GLIA_R2_ENDPOINT, GLIA_R2_BUCKET, GLIA_R2_ACCESS_KEY_ID, "
            "GLIA_R2_SECRET_ACCESS_KEY 가 필요합니다."
        )
    if public_base_url:
        raise ConfigError("공개 주소는 받지 않습니다. 버킷은 비공개로 둡니다.")
    if bucket == LMS_BUCKET:
        raise ConfigError("LMS 버킷 online-glia 에는 올리지 않습니다. HLS 전용 버킷이 필요합니다.")
    if _BUCKET.fullmatch(bucket) is None:
        raise ConfigError("버킷 이름은 소문자, 숫자, 하이픈만 사용할 수 있습니다.")
    _require_account_endpoint(endpoint)
    return R2Config(endpoint, bucket, access_key_id, secret_access_key, region)


def _require_account_endpoint(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".r2.cloudflarestorage.com")
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            "엔드포인트는 https://<계정>.r2.cloudflarestorage.com 형식만 받습니다. "
            "버킷 이름은 경로에 넣지 않습니다."
        )


def _parse_env_file(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.startswith("GLIA_R2_"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        parsed[key] = value
    return parsed
