import unittest

from glia_media.r2_layout import (
    LayoutError,
    playlist_object_key,
    segment_object_key,
    source_object_key,
)

ASSET = "a" * 32
VERSION = "b" * 32


class R2LayoutTest(unittest.TestCase):
    def test_hls_keys_stay_under_vod(self) -> None:
        source = source_object_key(ASSET, VERSION)
        playlist = playlist_object_key(ASSET, VERSION)
        segment = segment_object_key(ASSET, VERSION, "segment-000.ts")

        self.assertEqual(source, f"vod/originals/{ASSET}/{VERSION}/source.mp4")
        self.assertEqual(playlist, f"vod/packages/{ASSET}/{VERSION}/720p/index.m3u8")
        self.assertEqual(segment, f"vod/packages/{ASSET}/{VERSION}/720p/segment-000.ts")
        for key in (source, playlist, segment):
            self.assertFalse(key.startswith(("media/", "courses/", "coaching/", "online-glia/")))

    def test_rejects_ids_and_segment_names_that_leave_vod(self) -> None:
        with self.assertRaises(LayoutError):
            source_object_key("../media", VERSION)
        with self.assertRaises(LayoutError):
            segment_object_key(ASSET, VERSION, "../media/shared/original.mp4")
