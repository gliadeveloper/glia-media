import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from glia_media.jobs import load_job, run_pending
from glia_media.lms import LmsError, handle_request, job_view, lms_settings, notify_lms
from glia_media.serve import serve
from glia_media.version import VersionRecord


class _Publisher:
    def __call__(self, source: Path, library: Path, asset_id: str | None) -> VersionRecord:
        asset_id = asset_id or ("a" * 32)
        version_id = "b" * 32
        version_dir = library / asset_id / version_id
        version_dir.mkdir(parents=True)
        (version_dir / "manifest.json").write_text(
            json.dumps({"inspection": {"passed": True, "duration_seconds": 2.5}}),
            encoding="utf-8",
        )
        return VersionRecord(
            asset_id=asset_id,
            version_id=version_id,
            number=1,
            playlist=version_dir / "720p" / "index.m3u8",
            width=1280,
            height=720,
            duration_seconds=2.5,
            segment_count=1,
        )


class _Uploader:
    def __call__(self, library: Path, asset_id: str, version_id: str, config: object) -> None:
        return None


class LmsApiTest(unittest.TestCase):
    def test_request_and_status_stay_on_the_media_side(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "lesson.mp4"
            source.write_bytes(b"video")
            body = json.dumps(
                {"source_path": str(source), "lms_request_id": "lesson_request_1"}
            ).encode()
            status, created = handle_request(root, "POST", "/v1/jobs", "Bearer secret", body, "secret")
            self.assertEqual(status, 202)
            self.assertEqual(created["state"], "PROCESSING")
            self.assertEqual(created["lms_request_id"], "lesson_request_1")
            again, duplicate = handle_request(root, "POST", "/v1/jobs", "Bearer secret", body, "secret")
            self.assertEqual(again, 202)
            self.assertEqual(duplicate["job_id"], created["job_id"])

            run_pending(root, publish=_Publisher(), upload=_Uploader())
            status, view = handle_request(
                root,
                "GET",
                f"/v1/jobs/{created['job_id']}",
                "Bearer secret",
                b"",
                "secret",
            )
            self.assertEqual(status, 200)
            self.assertEqual(view["state"], "READY")
            self.assertEqual(view["duration_seconds"], 2.5)
            self.assertEqual(
                view["playlist_key"],
                f"vod/packages/{view['asset_id']}/{view['version_id']}/720p/index.m3u8",
            )

    def test_rejects_a_missing_or_wrong_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(LmsError) as error:
                handle_request(Path(temporary), "GET", "/v1/jobs/abc", "", b"", "secret")
            self.assertEqual(error.exception.status, 401)
            with self.assertRaises(LmsError):
                handle_request(Path(temporary), "GET", "/v1/jobs/abc", "Bearer other", b"", "secret")

    def test_callback_retries_without_putting_the_token_in_the_error(self) -> None:
        calls: list[str] = []

        def send(url: str, token: str, body: bytes) -> None:
            calls.append(token)
            if len(calls) == 1:
                raise LmsError(token, 500)

        notify_lms(
            "https://lms.example/hooks/media",
            "secret-token",
            {"state": "READY", "job_id": "a" * 32},
            send=send,
        )
        self.assertEqual(calls, ["secret-token", "secret-token"])

        def always_fail(url: str, token: str, body: bytes) -> None:
            raise LmsError(token, 500)

        with self.assertRaises(LmsError) as error:
            notify_lms(
                "https://lms.example/hooks/media",
                "secret-token",
                {"state": "FAILED", "error": "변환 실패"},
                send=always_fail,
            )
        self.assertNotIn("secret-token", error.exception.summary)

    def test_refuses_to_send_the_token_to_an_outside_http_address(self) -> None:
        def send(url: str, token: str, body: bytes) -> None:
            raise AssertionError(token)

        with self.assertRaises(LmsError):
            notify_lms("http://lms.example/hook", "secret-token", {"state": "READY"}, send=send)

    def test_failed_callback_leaves_the_finished_job_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "lesson.mp4"
            source.write_bytes(b"video")
            handle_request(
                root,
                "POST",
                "/v1/jobs",
                "Bearer secret",
                json.dumps({"source_path": str(source)}).encode(),
                "secret",
            )

            def on_finished(job: object) -> None:
                raise LmsError("LMS에 완료 알림을 보내지 못했습니다.")

            with self.assertRaises(LmsError):
                run_pending(root, publish=_Publisher(), upload=_Uploader(), on_finished=on_finished)
            stored = load_job(root, job_view(root, load_job(root, _only_job_id(root)))["job_id"])
            self.assertEqual(stored.state, "READY")

    def test_settings_do_not_carry_storage_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text(
                "GLIA_R2_SECRET_ACCESS_KEY=storage-secret\n"
                "GLIA_LMS_TOKEN=lms-token\n"
                "GLIA_PLAYBACK_BASE_URL=https://glia-media-playback.glia-media.workers.dev\n",
                encoding="utf-8",
            )
            settings = lms_settings(environ={}, env_file=env_file)
            self.assertEqual(settings["token"], "lms-token")
            self.assertEqual(settings["playback_base_url"], "https://glia-media-playback.glia-media.workers.dev")
            self.assertNotIn("storage-secret", json.dumps(settings))

    def test_local_server_accepts_a_job_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "lesson.mp4"
            source.write_bytes(b"video")
            ready = threading.Event()
            ports: list[int] = []

            def note(port: int) -> None:
                ports.append(port)
                ready.set()

            thread = threading.Thread(
                target=serve,
                args=(root, "secret"),
                kwargs={"host": "127.0.0.1", "port": 0, "ready": note},
                daemon=True,
            )
            thread.start()
            self.assertTrue(ready.wait(5))
            request = urllib.request.Request(
                f"http://127.0.0.1:{ports[0]}/v1/jobs",
                data=json.dumps({"source_path": str(source)}).encode(),
                method="POST",
                headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read().decode())
                self.assertEqual(response.status, 202)
            self.assertEqual(payload["state"], "PROCESSING")
            self.assertNotIn("secret", json.dumps(payload))
            denied = urllib.request.Request(
                f"http://127.0.0.1:{ports[0]}/v1/jobs/{payload['job_id']}",
                headers={"Authorization": "Bearer wrong"},
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(denied, timeout=2)
            self.assertEqual(error.exception.code, 401)


def _only_job_id(library: Path) -> str:
    import sqlite3

    connection = sqlite3.connect(library / "jobs.sqlite")
    row = connection.execute("SELECT id FROM jobs").fetchone()
    connection.close()
    if row is None:
        raise AssertionError("job missing")
    return str(row[0])
