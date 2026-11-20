"""큰 영상 파일을 한 번에 1MiB씩 읽는다."""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()
