import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from glia_media.convert import (
    ConvertError,
    convert,
    display_size,
    matches_720p,
    primary_video_stream,
    rotation_degrees,
    scale_filter,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)


class ScaleRuleTest(unittest.TestCase):
    def test_landscape_uses_height_720(self) -> None:
        self.assertEqual(scale_filter(1920, 1080), "scale=-2:720:flags=lanczos,setsar=1")
        self.assertTrue(matches_720p(1920, 1080, 1280, 720))

    def test_portrait_uses_width_720(self) -> None:
        self.assertEqual(scale_filter(1080, 1920), "scale=720:-2:flags=lanczos,setsar=1")
        self.assertTrue(matches_720p(1080, 1920, 720, 1280))

    def test_square_uses_height_720(self) -> None:
        self.assertEqual(scale_filter(1000, 1000), "scale=-2:720:flags=lanczos,setsar=1")

    def test_rotation_swaps_display_size(self) -> None:
        self.assertEqual(display_size(1920, 1080, 90), (1080, 1920))
        self.assertEqual(display_size(1920, 1080, -90), (1080, 1920))
        self.assertEqual(display_size(1920, 1080, 180), (1920, 1080))

    def test_rotation_comes_from_display_matrix_or_legacy_tag(self) -> None:
        self.assertEqual(rotation_degrees({"side_data_list": [{"rotation": 90}]}), 90)
        self.assertEqual(rotation_degrees({"tags": {"rotate": "-90"}}), -90)
        self.assertEqual(rotation_degrees({}), 0)

    def test_attached_picture_is_not_the_video(self) -> None:
        info = {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "disposition": {"attached_pic": 1},
                },
                {"index": 1, "codec_type": "video", "codec_name": "h264"},
            ]
        }
        self.assertEqual(primary_video_stream(info)["codec_name"], "h264")

    def test_rejects_cropped_or_odd_output(self) -> None:
        self.assertFalse(matches_720p(1920, 1080, 720, 720))
        self.assertFalse(matches_720p(1920, 1080, 1279, 720))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg가 없습니다")
class ConvertCliTest(unittest.TestCase):
    def test_landscape_and_portrait_keep_source_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            landscape = root / "landscape.mp4"
            portrait = root / "portrait.mp4"
            _make_mp4(landscape, 640, 360)
            _make_mp4(portrait, 360, 640)
            landscape_digest = _sha256(landscape)
            portrait_digest = _sha256(portrait)

            landscape_result = convert(landscape, root / "landscape-out")
            portrait_result = convert(portrait, root / "portrait-out")

            self.assertEqual(landscape_result.width, 1280)
            self.assertEqual(landscape_result.height, 720)
            self.assertEqual(portrait_result.width, 720)
            self.assertEqual(portrait_result.height, 1280)
            self.assertTrue((root / "landscape-out" / "index.m3u8").is_file())
            self.assertTrue((root / "landscape-out" / "segment-000.ts").is_file())
            self.assertGreaterEqual(landscape_result.segment_count, 1)
            self.assertEqual(_sha256(landscape), landscape_digest)
            self.assertEqual(_sha256(portrait), portrait_digest)

    def test_rotated_landscape_is_treated_as_portrait(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rotated.mp4"
            _make_mp4(root / "base.mp4", 640, 360)
            _run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-display_rotation:v:0",
                    "90",
                    "-i",
                    str(root / "base.mp4"),
                    "-c",
                    "copy",
                    str(source),
                ]
            )
            digest = _sha256(source)

            result = convert(source, root / "out")

            self.assertEqual((result.width, result.height), (720, 1280))
            self.assertEqual(_sha256(source), digest)

    def test_missing_audio_is_recorded_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "silent.mp4"
            _run(
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
                ]
            )
            digest = _sha256(source)
            output = root / "out"

            with self.assertRaises(ConvertError) as caught:
                convert(source, output)

            self.assertIn("오디오가 없습니다", caught.exception.summary)
            self.assertIsNone(caught.exception.record)
            self.assertFalse(output.exists())
            self.assertEqual(_sha256(source), digest)

    def test_non_empty_output_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.mp4"
            source.write_bytes(b"not a video")
            output = root / "out"
            output.mkdir()
            (output / "keep.txt").write_text("keep", encoding="utf-8")

            with self.assertRaises(ConvertError) as caught:
                convert(source, output)

            self.assertIn("비어 있지 않습니다", caught.exception.summary)
            self.assertEqual((output / "keep.txt").read_text(encoding="utf-8"), "keep")


def _make_mp4(path: Path, width: int, height: int) -> None:
    _run(
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
        ]
    )


if __name__ == "__main__":
    unittest.main()
