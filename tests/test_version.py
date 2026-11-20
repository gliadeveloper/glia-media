import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from glia_media.version import VersionError, publish_version, use_version


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(path: Path) -> dict[str, str]:
    return {
        str(file.relative_to(path)): _sha256(file)
        for file in sorted(path.rglob("*"))
        if file.is_file()
    }


def _make_mp4(path: Path, width: int, height: int) -> None:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={width}x{height}:rate=30:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg가 없습니다")
class VersionTest(unittest.TestCase):
    def test_new_version_keeps_the_previous_one_and_can_roll_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = root / "first.mp4"
            second = root / "second.mp4"
            _make_mp4(first, 640, 360)
            _make_mp4(second, 360, 640)
            library = root / "library"

            created = publish_version(first, library)
            first_dir = library / created.asset_id / created.version_id
            first_files = _tree(first_dir)
            manifest = json.loads((first_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(created.number, 1)
            self.assertEqual((created.width, created.height), (1280, 720))
            self.assertEqual(manifest["source"]["sha256"], _sha256(first))
            self.assertTrue(manifest["storage"]["source_key"].startswith("vod/originals/"))
            self.assertTrue(manifest["storage"]["playlist_key"].startswith("vod/packages/"))
            self.assertTrue(manifest["storage"]["segment_keys"][0].startswith("vod/packages/"))
            self.assertEqual(manifest["source"]["sha256"], _sha256(first_dir / "source.mp4"))
            self.assertTrue(manifest["inspection"]["passed"])
            self.assertEqual(_sha256(first), hashlib.sha256(first.read_bytes()).hexdigest())

            revised = publish_version(second, library, created.asset_id)
            asset = json.loads(
                (library / created.asset_id / "asset.json").read_text(encoding="utf-8")
            )

            self.assertEqual(revised.number, 2)
            self.assertEqual(revised.asset_id, created.asset_id)
            self.assertNotEqual(revised.version_id, created.version_id)
            self.assertEqual((revised.width, revised.height), (720, 1280))
            self.assertEqual(asset["current_version_id"], revised.version_id)
            self.assertEqual(_tree(first_dir), first_files)
            self.assertEqual(_sha256(first), manifest["source"]["sha256"])

            current = use_version(library, created.asset_id, created.version_id)
            asset = json.loads(
                (library / created.asset_id / "asset.json").read_text(encoding="utf-8")
            )
            self.assertEqual(current.version_id, created.version_id)
            self.assertEqual(asset["current_version_id"], created.version_id)
            self.assertEqual(_tree(first_dir), first_files)
            self.assertTrue((library / revised.asset_id / revised.version_id / "manifest.json").is_file())

    def test_failed_publish_does_not_create_a_version(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "silent.mp4"
            completed = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=320x240:rate=30:duration=1",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(source),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0)
            before = _sha256(source)

            with self.assertRaises(Exception) as caught:
                publish_version(source, root / "library")

            self.assertIn("오디오가 없습니다", str(caught.exception))
            self.assertEqual(_sha256(source), before)
            self.assertEqual(list((root / "library").glob("*/*/manifest.json")), [])

    def test_rejects_version_ids_that_escape_the_library(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(VersionError) as caught:
                use_version(Path(raw), "../outside", "a" * 32)
            self.assertIn("형식이 올바르지 않습니다", caught.exception.summary)
