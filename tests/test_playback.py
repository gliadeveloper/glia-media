import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from glia_media.lms import LmsError, handle_request

ASSET = "a" * 32
VERSION = "b" * 32
SECRET = "test-lms-token"
NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def _library(
    uploaded: bool = True,
    passed: bool = True,
    duration: float | None = None,
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    version_dir = root / ASSET / VERSION
    version_dir.mkdir(parents=True)
    playlist = f"vod/packages/{ASSET}/{VERSION}/720p/index.m3u8"
    segment = f"vod/packages/{ASSET}/{VERSION}/720p/segment-000.ts"
    inspection: dict[str, object] = {"passed": passed}
    if duration is not None:
        inspection["duration_seconds"] = duration
    (version_dir / "manifest.json").write_text(
        json.dumps(
            {
                "inspection": inspection,
                "storage": {"playlist_key": playlist, "segment_keys": [segment]},
            }
        ),
        encoding="utf-8",
    )
    receipt = {"uploaded": [playlist, segment], "already_present": []} if uploaded else {"uploaded": [], "already_present": []}
    (version_dir / "upload.json").write_text(json.dumps(receipt), encoding="utf-8")
    return temporary, root


class PlaybackGrantTest(unittest.TestCase):
    def test_grant_allows_playlist_and_segment_only(self) -> None:
        temporary, root = _library()
        self.addCleanup(temporary.cleanup)
        status, issued = handle_request(
            root,
            "POST",
            "/v1/playback",
            f"Bearer {SECRET}",
            json.dumps({"asset_id": ASSET, "version_id": VERSION}).encode(),
            SECRET,
            NOW,
        )
        self.assertEqual(status, 201)
        grant = str(issued["grant"])
        self.assertNotIn(SECRET, grant)
        self.assertTrue(str(issued["playlist_key"]).startswith("vod/packages/"))

        for name in ("index.m3u8", "segment-000.ts"):
            allowed, view = handle_request(
                root,
                "POST",
                "/v1/playback/authorize",
                "",
                json.dumps({"grant": grant, "name": name}).encode(),
                SECRET,
                NOW,
            )
            self.assertEqual(allowed, 200)
            self.assertTrue(str(view["key"]).startswith("vod/packages/"))
            self.assertNotIn("source.mp4", str(view["key"]))

        with self.assertRaises(LmsError) as source:
            handle_request(
                root,
                "POST",
                "/v1/playback/authorize",
                "",
                json.dumps({"grant": grant, "name": "source.mp4"}).encode(),
                SECRET,
                NOW,
            )
        self.assertEqual(source.exception.status, 403)

        with self.assertRaises(LmsError):
            handle_request(
                root,
                "POST",
                "/v1/playback/authorize",
                "",
                json.dumps({"grant": grant, "name": "../media/shared/original.mp4"}).encode(),
                SECRET,
                NOW,
            )

    def test_expired_or_tampered_grant_is_rejected(self) -> None:
        temporary, root = _library()
        self.addCleanup(temporary.cleanup)
        _, issued = handle_request(
            root,
            "POST",
            "/v1/playback",
            f"Bearer {SECRET}",
            json.dumps({"asset_id": ASSET, "version_id": VERSION}).encode(),
            SECRET,
            NOW,
        )
        grant = str(issued["grant"])
        with self.assertRaises(LmsError) as expired:
            handle_request(
                root,
                "POST",
                "/v1/playback/authorize",
                "",
                json.dumps({"grant": grant, "name": "index.m3u8"}).encode(),
                SECRET,
                NOW + timedelta(minutes=11),
            )
        self.assertEqual(expired.exception.status, 401)
        tampered = grant[:-1] + ("A" if grant[-1] != "A" else "B")
        with self.assertRaises(LmsError) as broken:
            handle_request(
                root,
                "POST",
                "/v1/playback/authorize",
                "",
                json.dumps({"grant": tampered, "name": "index.m3u8"}).encode(),
                SECRET,
                NOW,
            )
        self.assertEqual(broken.exception.status, 401)

    def test_issue_requires_the_lms_token_and_a_published_version(self) -> None:
        temporary, root = _library(uploaded=False)
        self.addCleanup(temporary.cleanup)
        with self.assertRaises(LmsError) as denied:
            handle_request(
                root,
                "POST",
                "/v1/playback",
                "",
                json.dumps({"asset_id": ASSET, "version_id": VERSION}).encode(),
                SECRET,
                NOW,
            )
        self.assertEqual(denied.exception.status, 401)
        with self.assertRaises(LmsError) as unpublished:
            handle_request(
                root,
                "POST",
                "/v1/playback",
                f"Bearer {SECRET}",
                json.dumps({"asset_id": ASSET, "version_id": VERSION}).encode(),
                SECRET,
                NOW,
            )
        self.assertIn("저장소", unpublished.exception.summary)

    def test_playlist_url_lasts_for_the_video(self) -> None:
        temporary, root = _library(duration=3600)
        self.addCleanup(temporary.cleanup)
        base = "https://glia-media-playback.glia-media.workers.dev"
        status, issued = handle_request(
            root,
            "POST",
            "/v1/playback",
            f"Bearer {SECRET}",
            json.dumps({"asset_id": ASSET, "version_id": VERSION}).encode(),
            SECRET,
            NOW,
            playback_base=base,
        )
        self.assertEqual(status, 201)
        self.assertEqual(issued["expires_at"], "2026-11-20T01:30:00Z")
        playlist_url = str(issued["playlist_url"])
        self.assertTrue(playlist_url.startswith(f"{base}/p/"))
        self.assertTrue(playlist_url.endswith(f"/{issued['playlist_key']}"))
        grant = str(issued["grant"])
        allowed, _view = handle_request(
            root,
            "POST",
            "/v1/playback/authorize",
            "",
            json.dumps({"grant": grant, "name": "index.m3u8"}).encode(),
            SECRET,
            NOW + timedelta(minutes=11),
        )
        self.assertEqual(allowed, 200)
        with self.assertRaises(LmsError) as expired:
            handle_request(
                root,
                "POST",
                "/v1/playback/authorize",
                "",
                json.dumps({"grant": grant, "name": "index.m3u8"}).encode(),
                SECRET,
                NOW + timedelta(seconds=5400),
            )
        self.assertEqual(expired.exception.status, 401)
        with self.assertRaises(LmsError) as insecure:
            handle_request(
                root,
                "POST",
                "/v1/playback",
                f"Bearer {SECRET}",
                json.dumps({"asset_id": ASSET, "version_id": VERSION}).encode(),
                SECRET,
                NOW,
                playback_base="http://media.example",
            )
        self.assertEqual(insecure.exception.status, 503)
