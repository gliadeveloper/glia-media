"""변환 한 번을 작업 한 건으로 남긴다.

기록은 보관 폴더의 jobs.sqlite 다. LMS 데이터베이스는 쓰지 않는다.
상태는 REQUESTED, TRANSCODING, VALIDATING, UPLOADING, READY 로 가고,
실패하면 FAILED 로 남는다. 같은 원본은 작업을 새로 만들지 않는다.
선점이 살아있는 동안에는 다른 실행이 같은 작업을 가져가지 못한다.
워커는 이 표에서 기한이 된 작업을 하나 선점해 처리하고, 실패하면 잠시 뒤에 다시 집는다.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from glia_media.convert import ConvertError
from glia_media.r2_config import ConfigError, R2Config, load_r2_config
from glia_media.r2_upload import UploadError, upload_version
from glia_media.version import VersionError, publish_version

LEASE_SECONDS = 15 * 60
MAX_ATTEMPTS = 3
_HEARTBEAT_SECONDS = 30
_BACKOFF_SECONDS = (60, 300, 900)
_ID = re.compile(r"^[0-9a-f]{32}$")
_LMS_REQUEST = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_ACTIVE = ("REQUESTED", "TRANSCODING", "VALIDATING", "UPLOADING", "FAILED")


class JobError(Exception):
    def __init__(self, summary: str) -> None:
        self.summary = summary
        super().__init__(summary)


@dataclass(frozen=True)
class Job:
    id: str
    asset_id: str | None
    version_id: str | None
    source_sha256: str
    state: str
    attempt_count: int
    last_error: str | None
    failed_during: str | None
    lms_request_id: str | None = None


def request_job(
    library: Path,
    source: Path,
    asset_id: str | None = None,
    lms_request_id: str | None = None,
) -> Job:
    library = library.expanduser().resolve()
    source = source.expanduser().resolve()
    if asset_id is not None and _ID.fullmatch(asset_id) is None:
        raise JobError("영상 번호 형식이 올바르지 않습니다.")
    if lms_request_id is not None and _LMS_REQUEST.fullmatch(lms_request_id) is None:
        raise JobError("LMS 요청 번호 형식이 올바르지 않습니다.")
    if not source.is_file() or source.suffix.lower() != ".mp4":
        raise JobError("작업으로 남길 원본 MP4가 필요합니다.")
    digest = _sha256(source)
    with _connection(library) as connection:
        _begin(connection)
        existing = connection.execute(
            "SELECT * FROM jobs WHERE source_sha256 = ?",
            (digest,),
        ).fetchone()
        if existing is not None:
            if asset_id is not None and existing["asset_id"] not in {None, asset_id}:
                _rollback(connection)
                raise JobError("이 원본은 이미 다른 영상 작업에 있습니다.")
            if asset_id is not None and existing["asset_id"] is None:
                connection.execute(
                    "UPDATE jobs SET asset_id = ? WHERE id = ?",
                    (asset_id, existing["id"]),
                )
            if lms_request_id is not None and existing["lms_request_id"] is None:
                connection.execute(
                    "UPDATE jobs SET lms_request_id = ? WHERE id = ?",
                    (lms_request_id, existing["id"]),
                )
            _commit(connection)
            stored = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (existing["id"],),
            ).fetchone()
            if stored is None:
                raise JobError("작업 기록을 읽지 못했습니다.")
            return _job(stored)
        job_id = uuid.uuid4().hex
        now = _iso(_clock_now(None))
        connection.execute(
            """
            INSERT INTO jobs (
                id, asset_id, version_id, source_path, source_sha256, state,
                failed_during, lease_token, lease_expires_at, attempt_count,
                last_error, created_at, updated_at, completed_at, next_attempt_at,
                lms_request_id
            ) VALUES (?, ?, NULL, ?, ?, 'REQUESTED', NULL, NULL, NULL, 0, NULL, ?, ?, NULL, ?, ?)
            """,
            (job_id, asset_id, str(source), digest, now, now, now, lms_request_id),
        )
        _event(connection, job_id, "REQUESTED", None, now)
        _commit(connection)
        stored = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if stored is None:
        raise JobError("작업 기록을 만들지 못했습니다.")
    return _job(stored)


def run_job(
    library: Path,
    job_id: str,
    publish: Callable[..., object] = publish_version,
    upload: Callable[..., object] | None = None,
    config: R2Config | None = None,
    env_file: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    owner: str | None = None,
) -> Job:
    library = library.expanduser().resolve()
    if _ID.fullmatch(job_id) is None:
        raise JobError("작업 번호 형식이 올바르지 않습니다.")
    token = uuid.uuid4().hex
    holder = owner or token
    job = _claim(library, job_id, holder, clock)
    if job.state == "READY":
        return job
    stop = threading.Event()
    beater = threading.Thread(
        target=_heartbeat,
        args=(library, job_id, holder, clock, stop),
        daemon=True,
    )
    beater.start()
    try:
        return _execute(library, job, holder, publish, upload, config, env_file, clock)
    finally:
        stop.set()
        beater.join(timeout=2)


def run_pending(
    library: Path,
    publish: Callable[..., object] = publish_version,
    upload: Callable[..., object] | None = None,
    config: R2Config | None = None,
    env_file: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    owner: str | None = None,
    on_finished: Callable[[Job], None] | None = None,
) -> list[Job]:
    """기한이 된 작업을 만들어진 순서대로 처리한다. 대기 중인 작업이 없으면 빈 목록이다."""
    library = library.expanduser().resolve()
    finished: list[Job] = []
    skipped: set[str] = set()
    while True:
        job_id = _due_job_id(library, clock, skipped)
        if job_id is None:
            return finished
        try:
            finished.append(
                run_job(
                    library,
                    job_id,
                    publish=publish,
                    upload=upload,
                    config=config,
                    env_file=env_file,
                    clock=clock,
                    owner=owner,
                )
            )
        except JobError as error:
            if "다른 실행" in error.summary or "선점이 풀렸습니다" in error.summary:
                skipped.add(job_id)
                continue
            failed = _load_job(library, job_id)
            finished.append(failed)
            if on_finished is not None:
                on_finished(failed)
            if failed.state == "FAILED":
                skipped.add(job_id)
            continue
        if on_finished is not None:
            on_finished(finished[-1])


def job_states(library: Path, job_id: str) -> list[str]:
    library = library.expanduser().resolve()
    with _connection(library) as connection:
        rows = connection.execute(
            "SELECT state FROM job_events WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
    return [row["state"] for row in rows]


def _execute(
    library: Path,
    job: Job,
    token: str,
    publish: Callable[..., object],
    upload: Callable[..., object] | None,
    config: R2Config | None,
    env_file: Path | None,
    clock: Callable[[], datetime] | None,
) -> Job:
    try:
        if job.version_id is None:
            source = Path(str(_column(library, job.id, "source_path")))
            if _sha256(source) != job.source_sha256:
                raise JobError("원본 파일이 작업 기록과 다릅니다.")
            _transition(library, job.id, token, "TRANSCODING", clock)
            record = publish(source, library, job.asset_id)
            asset_id = getattr(record, "asset_id", None)
            version_id = getattr(record, "version_id", None)
            if not isinstance(asset_id, str) or not isinstance(version_id, str):
                raise JobError("변환 결과에 영상 번호가 없습니다.")
            _save_version(library, job.id, token, asset_id, version_id, clock)
        else:
            asset_id = job.asset_id
            version_id = job.version_id
        if asset_id is None or version_id is None:
            raise JobError("검사할 버전 번호가 없습니다.")
        if job.version_id is None or job.state != "VALIDATING":
            _transition(library, job.id, token, "VALIDATING", clock)
        _require_passed(library, asset_id, version_id)
        _transition(library, job.id, token, "UPLOADING", clock)
        if upload is None:
            sender = upload_version
            active_config = config if config is not None else load_r2_config(env_file=env_file)
        else:
            sender = upload
            active_config = config
        sender(library, asset_id, version_id, active_config)
        return _transition(library, job.id, token, "READY", clock)
    except JobError as error:
        _fail(library, job.id, token, error.summary, clock)
        raise
    except (ConvertError, VersionError, UploadError, ConfigError) as error:
        _fail(library, job.id, token, error.summary, clock)
        raise JobError(error.summary) from None


def _claim(
    library: Path,
    job_id: str,
    token: str,
    clock: Callable[[], datetime] | None,
) -> Job:
    now = _iso(_clock_now(clock))
    expires = _iso(_clock_now(clock) + timedelta(seconds=LEASE_SECONDS))
    with _connection(library) as connection:
        _begin(connection)
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            _rollback(connection)
            raise JobError("작업 기록이 없습니다.")
        if row["state"] == "READY":
            _commit(connection)
            return _job(row)
        if row["state"] not in _ACTIVE:
            _rollback(connection)
            raise JobError("이 상태로 작업을 시작할 수 없습니다.")
        lease_expires = row["lease_expires_at"]
        if row["lease_token"] and isinstance(lease_expires, str) and lease_expires > now:
            _rollback(connection)
            raise JobError("다른 실행이 이 작업을 처리하고 있습니다.")
        resumed = "VALIDATING" if row["version_id"] else "REQUESTED"
        state = resumed if row["state"] == "FAILED" else row["state"]
        connection.execute(
            """
            UPDATE jobs
            SET state = ?, lease_token = ?, lease_expires_at = ?, attempt_count = attempt_count + 1,
                last_error = NULL, failed_during = NULL, updated_at = ?
            WHERE id = ?
            """,
            (state, token, expires, now, job_id),
        )
        if state != row["state"]:
            _event(connection, job_id, state, None, now)
        _commit(connection)
        stored = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if stored is None:
        raise JobError("작업 기록을 읽지 못했습니다.")
    return _job(stored)


def _heartbeat(
    library: Path,
    job_id: str,
    token: str,
    clock: Callable[[], datetime] | None,
    stop: threading.Event,
) -> None:
    while not stop.wait(_HEARTBEAT_SECONDS):
        expires = _iso(_clock_now(clock) + timedelta(seconds=LEASE_SECONDS))
        now = _iso(_clock_now(clock))
        with _connection(library) as connection:
            _begin(connection)
            connection.execute(
                """
                UPDATE jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND lease_token = ? AND state != 'READY' AND state != 'FAILED'
                """,
                (expires, now, job_id, token),
            )
            _commit(connection)


def _transition(
    library: Path,
    job_id: str,
    token: str,
    state: str,
    clock: Callable[[], datetime] | None,
) -> Job:
    now = _iso(_clock_now(clock))
    expires = _iso(_clock_now(clock) + timedelta(seconds=LEASE_SECONDS))
    completed = now if state == "READY" else None
    with _connection(library) as connection:
        _begin(connection)
        updated = connection.execute(
            """
            UPDATE jobs
            SET state = ?, lease_token = ?, lease_expires_at = ?, updated_at = ?, completed_at = ?,
                last_error = NULL, failed_during = NULL
            WHERE id = ? AND lease_token = ? AND state != 'READY'
            """,
            (state, None if state == "READY" else token, None if state == "READY" else expires, now, completed, job_id, token),
        )
        if updated.rowcount != 1:
            _rollback(connection)
            raise JobError("작업 선점이 풀렸습니다. 상태는 바꾸지 않았습니다.")
        _event(connection, job_id, state, None, now)
        _commit(connection)
        stored = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if stored is None:
        raise JobError("작업 기록을 읽지 못했습니다.")
    return _job(stored)


def _save_version(
    library: Path,
    job_id: str,
    token: str,
    asset_id: str,
    version_id: str,
    clock: Callable[[], datetime] | None,
) -> None:
    now = _iso(_clock_now(clock))
    with _connection(library) as connection:
        _begin(connection)
        updated = connection.execute(
            """
            UPDATE jobs
            SET asset_id = ?, version_id = ?, updated_at = ?
            WHERE id = ? AND lease_token = ?
            """,
            (asset_id, version_id, now, job_id, token),
        )
        if updated.rowcount != 1:
            _rollback(connection)
            raise JobError("작업 선점이 풀렸습니다. 버전 번호는 기록하지 않았습니다.")
        _commit(connection)


def _fail(
    library: Path,
    job_id: str,
    token: str,
    summary: str,
    clock: Callable[[], datetime] | None,
) -> None:
    now = _iso(_clock_now(clock))
    with _connection(library) as connection:
        _begin(connection)
        current = connection.execute(
            "SELECT state, attempt_count FROM jobs WHERE id = ? AND lease_token = ?",
            (job_id, token),
        ).fetchone()
        if current is None or current["state"] in {"READY", "FAILED"}:
            _rollback(connection)
            return
        delay = _BACKOFF_SECONDS[min(int(current["attempt_count"]), len(_BACKOFF_SECONDS)) - 1]
        next_attempt = _iso(_clock_now(clock) + timedelta(seconds=delay))
        connection.execute(
            """
            UPDATE jobs
            SET state = 'FAILED', failed_during = ?, last_error = ?, lease_token = NULL,
                lease_expires_at = NULL, updated_at = ?, next_attempt_at = ?
            WHERE id = ? AND lease_token = ?
            """,
            (current["state"], summary, now, next_attempt, job_id, token),
        )
        _event(connection, job_id, "FAILED", summary, now)
        _commit(connection)


def _require_passed(library: Path, asset_id: str, version_id: str) -> None:
    manifest_path = library / asset_id / version_id / "manifest.json"
    if not manifest_path.is_file():
        raise JobError("검사할 버전 기록이 없습니다.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("inspection", {}).get("passed"):
        raise JobError("검사를 통과하지 않은 버전은 올리지 않습니다.")


def _column(library: Path, job_id: str, name: str) -> object:
    with _connection(library) as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise JobError("작업 기록이 없습니다.")
    return row[name]


@contextmanager
def _connection(library: Path):
    library.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(library / "jobs.sqlite", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.isolation_level = None
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                asset_id TEXT,
                version_id TEXT,
                source_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                failed_during TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                attempt_count INTEGER NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        _ensure_columns(connection)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                state TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        yield connection
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()


def _ensure_columns(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    if "next_attempt_at" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN next_attempt_at TEXT")
    if "lms_request_id" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN lms_request_id TEXT")


def _due_job_id(
    library: Path,
    clock: Callable[[], datetime] | None,
    skip: set[str],
) -> str | None:
    now = _iso(_clock_now(clock))
    clauses = [
        "state != 'READY'",
        "(lease_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?)",
        """(
            state != 'FAILED'
            OR (attempt_count < ? AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
        )""",
    ]
    parameters: list[object] = [now, MAX_ATTEMPTS, now]
    if skip:
        clauses.append(f"id NOT IN ({','.join('?' for _ in skip)})")
        parameters.extend(sorted(skip))
    query = f"SELECT id FROM jobs WHERE {' AND '.join(clauses)} ORDER BY rowid LIMIT 1"
    with _connection(library) as connection:
        row = connection.execute(query, parameters).fetchone()
    if row is None:
        return None
    return str(row["id"])


def load_job(library: Path, job_id: str) -> Job:
    return _load_job(library, job_id)


def _load_job(library: Path, job_id: str) -> Job:
    with _connection(library) as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise JobError("작업 기록이 없습니다.")
    return _job(row)


def _event(
    connection: sqlite3.Connection,
    job_id: str,
    state: str,
    detail: str | None,
    now: str,
) -> None:
    connection.execute(
        "INSERT INTO job_events (job_id, state, detail, created_at) VALUES (?, ?, ?, ?)",
        (job_id, state, detail, now),
    )


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        asset_id=row["asset_id"],
        version_id=row["version_id"],
        source_sha256=row["source_sha256"],
        state=row["state"],
        attempt_count=int(row["attempt_count"]),
        last_error=row["last_error"],
        failed_during=row["failed_during"],
        lms_request_id=row["lms_request_id"],
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clock_now(clock: Callable[[], datetime] | None) -> datetime:
    current = datetime.now(timezone.utc) if clock is None else clock()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _begin(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")


def _commit(connection: sqlite3.Connection) -> None:
    connection.execute("COMMIT")


def _rollback(connection: sqlite3.Connection) -> None:
    connection.execute("ROLLBACK")
