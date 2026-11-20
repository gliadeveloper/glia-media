"""로컬 .env 에서 GLIA 설정만 읽는다.

프로세스에 이미 있는 값은 여기서 덮지 않는다. 그 판단은 호출하는 쪽이 한다.
"""

from __future__ import annotations

from pathlib import Path


def parse_env_file(path: Path, prefixes: tuple[str, ...]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not any(key.startswith(prefix) for prefix in prefixes):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        parsed[key] = value
    return parsed
