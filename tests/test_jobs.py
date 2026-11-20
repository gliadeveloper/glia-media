import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from glia_media.jobs import JobError, job_states, request_job, run_job, run_pending
from glia_media.version import VersionRecord


def _source(root: Path, name: str, payload: bytes) -> Path:
    path = root / name
    path.write_bytes(payload)
    return path


def _passed_manifest(library: Path, asset_id: str, version_id: str) -> None:
    version_dir = library / asset_id / version_id
    version_dir.mkdir(parents=True)
    (version_dir / "manifest.json").write_text(
        json.dumps({"inspection": {"passed": True}}),
        encoding="utf-8",
    )


class _Publisher:
    def __init__(self, library: Path, passed: bool = True) -> None:
        self.library = library
        self.passed = passed
        self.calls = 0

    def __call__(self, source: Path, library: Path, asset_id: str | None) -> VersionRecord:
        self.calls += 1
        asset_id = asset_id or "c" * 32
        version_id = f"{self.calls:032x}"[-32:]
        version_dir = library / asset_id / version_id
        version_dir.mkdir(parents=True)
        (version_dir / "manifest.json").write_text(
            json.dumps({"inspection": {"passed": self.passed}}),
            encoding="utf-8",
        )
        return VersionRecord(
            asset_id=asset_id,
            version_id=version_id,
            number=1,
            playlist=version_dir / "720p" / "index.m3u8",
            width=1280,
            height=720,
            duration_seconds=1,
            segment_count=1,
        )


class _Uploader:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def __call__(self, library: Path, asset_id: str, version_id: str, config: object) -> None:
        self.calls += 1
        if self.fail:
            raise JobError("업로드가 끊겼습니다.")


class WorkerTest(unittest.TestCase):
    def test_runs_due_jobs_in_creation_order_and_stops_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = _source(root, "first.mp4", b"first")
            second = _source(root, "second.mp4", b"second")
            older = request_job(root, first)
            newer = request_job(root, second)
            seen: list[str] = []
            publisher = _Publisher(root)

            def publish(source: Path, library: Path, asset_id: str | None) -> VersionRecord:
                seen.append(source.name)
                return publisher(source, library, asset_id)

            finished = run_pending(root, publish=publish, upload=_Uploader())
            self.assertEqual([job.id for job in finished], [older.id, newer.id])
            self.assertEqual(seen, ["first.mp4", "second.mp4"])
            self.assertEqual(run_pending(root, publish=publish, upload=_Uploader()), [])

    def test_failure_does_not_block_the_next_job_or_retry_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            moment = datetime(2026, 11, 20, tzinfo=timezone.utc)
            broken = request_job(root, _source(root, "broken.mp4", b"broken"))
            healthy = request_job(root, _source(root, "healthy.mp4", b"healthy"))
            publisher = _Publisher(root)
            uploader = _Uploader(fail=True)

            def upload(library: Path, asset_id: str, version_id: str, config: object) -> None:
                if asset_id == "c" * 32 and uploader.calls == 0:
                    uploader(library, asset_id, version_id, config)
                uploader.fail = False
                uploader(library, asset_id, version_id, config)

            finished = run_pending(
                root,
                publish=publisher,
                upload=upload,
                clock=lambda: moment,
            )
            self.assertEqual(finished[0].id, broken.id)
            self.assertEqual(finished[0].state, "FAILED")
            self.assertEqual(finished[1].id, healthy.id)
            self.assertEqual(finished[1].state, "READY")
            self.assertEqual(
                run_pending(root, publish=publisher, upload=_Uploader(), clock=lambda: moment),
                [],
            )

            later = moment + timedelta(seconds=61)
            retried = run_pending(
                root,
                publish=publisher,
                upload=_Uploader(),
                clock=lambda: later,
            )
            self.assertEqual([job.id for job in retried], [broken.id])
            self.assertEqual(retried[0].state, "READY")
            self.assertEqual(publisher.calls, 2)


class JobRecordTest(unittest.TestCase):
    def test_same_source_keeps_one_job_and_reaches_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source(root, "lesson.mp4", b"same-bytes")
            publisher = _Publisher(root)
            uploader = _Uploader()
            first = request_job(root, source)
            second = request_job(root, _source(root, "copy.mp4", b"same-bytes"))
            finished = run_job(root, first.id, publish=publisher, upload=uploader)

            self.assertEqual(first.id, second.id)
            self.assertEqual(finished.state, "READY")
            self.assertEqual(publisher.calls, 1)
            self.assertEqual(uploader.calls, 1)
            self.assertEqual(
                job_states(root, first.id),
                ["REQUESTED", "TRANSCODING", "VALIDATING", "UPLOADING", "READY"],
            )
            reopened = request_job(root, source)
            self.assertEqual(reopened.state, "READY")
            self.assertEqual(publisher.calls, 1)

    def test_failed_upload_retries_without_converting_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source(root, "lesson.mp4", b"video")
            publisher = _Publisher(root)
            failing = _Uploader(fail=True)
            job = request_job(root, source)
            with self.assertRaises(JobError):
                run_job(root, job.id, publish=publisher, upload=failing)
            failed = request_job(root, source)
            self.assertEqual(failed.state, "FAILED")
            self.assertEqual(failed.failed_during, "UPLOADING")
            self.assertIsNotNone(failed.version_id)

            uploader = _Uploader()
            finished = run_job(root, job.id, publish=publisher, upload=uploader)
            self.assertEqual(finished.state, "READY")
            self.assertEqual(publisher.calls, 1)
            self.assertEqual(uploader.calls, 1)

    def test_failed_inspection_does_not_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source(root, "lesson.mp4", b"video")
            publisher = _Publisher(root, passed=False)
            uploader = _Uploader()
            job = request_job(root, source)
            with self.assertRaises(JobError) as error:
                run_job(root, job.id, publish=publisher, upload=uploader)
            self.assertIn("검사", error.exception.summary)
            self.assertEqual(uploader.calls, 0)
            self.assertEqual(request_job(root, source).failed_during, "VALIDATING")

    def test_live_lease_blocks_a_second_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source(root, "lesson.mp4", b"video")
            job = request_job(root, source)
            moment = datetime(2026, 11, 20, tzinfo=timezone.utc)
            held_until = (moment + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
            connection = sqlite3.connect(root / "jobs.sqlite")
            connection.execute(
                "UPDATE jobs SET state = 'TRANSCODING', lease_token = 'other', lease_expires_at = ? WHERE id = ?",
                (held_until, job.id),
            )
            connection.commit()
            connection.close()
            with self.assertRaises(JobError) as error:
                run_job(root, job.id, clock=lambda: moment)
            self.assertIn("다른 실행", error.exception.summary)

    def test_expired_lease_resumes_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source(root, "lesson.mp4", b"video")
            job = request_job(root, source)
            asset_id = "d" * 32
            version_id = "e" * 32
            _passed_manifest(root, asset_id, version_id)
            moment = datetime(2026, 11, 20, tzinfo=timezone.utc)
            expired = (moment - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            connection = sqlite3.connect(root / "jobs.sqlite")
            connection.execute(
                """
                UPDATE jobs
                SET state = 'UPLOADING', asset_id = ?, version_id = ?, lease_token = 'dead',
                    lease_expires_at = ?
                WHERE id = ?
                """,
                (asset_id, version_id, expired, job.id),
            )
            connection.commit()
            connection.close()
            publisher = _Publisher(root)
            uploader = _Uploader()
            finished = run_job(
                root,
                job.id,
                publish=publisher,
                upload=uploader,
                clock=lambda: moment,
            )
            self.assertEqual(finished.state, "READY")
            self.assertEqual(publisher.calls, 0)
            self.assertEqual(uploader.calls, 1)

    def test_changed_source_is_not_converted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source(root, "lesson.mp4", b"before")
            job = request_job(root, source)
            source.write_bytes(b"after")
            publisher = _Publisher(root)
            with self.assertRaises(JobError) as error:
                run_job(root, job.id, publish=publisher, upload=_Uploader())
            self.assertIn("원본 파일", error.exception.summary)
            self.assertEqual(publisher.calls, 0)
            self.assertEqual(request_job(root, _source(root, "other.mp4", b"before")).id, job.id)
