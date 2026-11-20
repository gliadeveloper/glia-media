import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from glia_media.r2_config import ConfigError, load_r2_config
from glia_media.r2_upload import (
    PlannedObject,
    PreconditionFailed,
    UploadError,
    plan_version_upload,
    upload_planned,
    upload_version,
)

ASSET = "a" * 32
VERSION = "b" * 32
ENDPOINT = "https://example.r2.cloudflarestorage.com"


def _settings(**overrides: str) -> dict[str, str]:
    values = {
        "GLIA_R2_ENDPOINT": ENDPOINT,
        "GLIA_R2_BUCKET": "glia-media",
        "GLIA_R2_ACCESS_KEY_ID": "key",
        "GLIA_R2_SECRET_ACCESS_KEY": "secret",
    }
    values.update(overrides)
    return values


class ConfigTest(unittest.TestCase):
    def test_rejects_lms_bucket_and_public_url(self) -> None:
        with self.assertRaises(ConfigError) as lms:
            load_r2_config(_settings(GLIA_R2_BUCKET="online-glia"), env_file=None)
        self.assertIn("online-glia", lms.exception.summary)

        with self.assertRaises(ConfigError) as public:
            load_r2_config(_settings(GLIA_R2_PUBLIC_BASE_URL="https://cdn.example"), env_file=None)
        self.assertIn("비공개", public.exception.summary)

    def test_rejects_endpoint_that_includes_a_bucket_path(self) -> None:
        with self.assertRaises(ConfigError):
            load_r2_config(
                _settings(GLIA_R2_ENDPOINT=f"{ENDPOINT}/online-glia"),
                env_file=None,
            )

    def test_reads_glia_keys_from_env_file_without_overriding_the_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "R2_BUCKET=online-glia",
                        "GLIA_R2_BUCKET=from-file",
                        "GLIA_R2_SECRET_ACCESS_KEY='from-file-secret'",
                        "GLIA_R2_ENDPOINT=" + ENDPOINT,
                        "GLIA_R2_ACCESS_KEY_ID=from-file-key",
                    ]
                ),
                encoding="utf-8",
            )
            config = load_r2_config(
                {"GLIA_R2_BUCKET": "from-process"},
                env_file,
            )
        self.assertEqual(config.bucket, "from-process")
        self.assertEqual(config.secret_access_key, "from-file-secret")
        self.assertNotIn("online-glia", config.bucket)


class _MemoryStore:
    def __init__(self, sizes: dict[str, int] | None = None) -> None:
        self.sizes = dict(sizes or {})
        self.puts: list[str] = []
        self.conflict_on_put: set[str] = set()

    def head_size(self, key: str) -> int | None:
        return self.sizes.get(key)

    def put_new(self, item: PlannedObject) -> None:
        if item.key in self.conflict_on_put:
            self.sizes[item.key] = item.size
            raise PreconditionFailed
        self.puts.append(item.key)
        self.sizes[item.key] = item.size


def _library() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    version_dir = root / ASSET / VERSION
    package = version_dir / "720p"
    package.mkdir(parents=True)
    (version_dir / "source.mp4").write_bytes(b"source")
    (package / "segment-000.ts").write_bytes(b"segment")
    (package / "index.m3u8").write_bytes(b"playlist")
    (root / ASSET / "asset.json").write_text(
        json.dumps({"current_version_id": VERSION}),
        encoding="utf-8",
    )
    storage = {
        "source_key": f"vod/originals/{ASSET}/{VERSION}/source.mp4",
        "playlist_key": f"vod/packages/{ASSET}/{VERSION}/720p/index.m3u8",
        "segment_keys": [f"vod/packages/{ASSET}/{VERSION}/720p/segment-000.ts"],
    }
    manifest = {
        "asset_id": ASSET,
        "version_id": VERSION,
        "inspection": {"passed": True},
        "storage": storage,
    }
    (version_dir / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return temporary, root


class UploadPlanTest(unittest.TestCase):
    def test_uploads_segments_then_source_then_playlist_and_skips_same_size(self) -> None:
        temporary, root = _library()
        self.addCleanup(temporary.cleanup)
        planned = plan_version_upload(root, ASSET, VERSION)
        self.assertTrue(all(item.key.startswith("vod/") for item in planned))
        self.assertTrue(planned[-1].key.endswith("index.m3u8"))
        self.assertTrue(planned[-2].key.endswith("source.mp4"))

        playlist_key = planned[-1].key
        store = _MemoryStore({playlist_key: planned[-1].size})
        result = upload_planned(planned, store, "glia-media")
        self.assertEqual(store.puts[-1], planned[-2].key)
        self.assertNotIn(playlist_key, store.puts)
        self.assertEqual(result.already_present, [playlist_key])
        self.assertEqual(len(result.uploaded), 2)

    def test_refuses_to_overwrite_a_different_object(self) -> None:
        temporary, root = _library()
        self.addCleanup(temporary.cleanup)
        planned = plan_version_upload(root, ASSET, VERSION)
        store = _MemoryStore({planned[0].key: planned[0].size + 9})
        with self.assertRaises(UploadError) as error:
            upload_planned(planned, store, "glia-media")
        self.assertIn("덮어쓰지 않았습니다", error.exception.summary)
        self.assertEqual(store.puts, [])

    def test_treats_a_lost_create_race_as_already_present_when_size_matches(self) -> None:
        temporary, root = _library()
        self.addCleanup(temporary.cleanup)
        planned = plan_version_upload(root, ASSET, VERSION)
        store = _MemoryStore()
        store.conflict_on_put.add(planned[0].key)
        result = upload_planned(planned, store, "glia-media")
        self.assertEqual(result.already_present, [planned[0].key])
        self.assertNotIn(planned[0].key, store.puts)
        self.assertEqual(len(result.uploaded), 2)

    def test_rejects_a_manifest_that_points_outside_vod(self) -> None:
        temporary, root = _library()
        self.addCleanup(temporary.cleanup)
        manifest_path = root / ASSET / VERSION / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["storage"]["source_key"] = f"media/shared/{ASSET}/original.mp4"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(UploadError) as error:
            plan_version_upload(root, ASSET, VERSION)
        self.assertIn("vod/", error.exception.summary)

    def test_receipt_records_bucket_without_calling_the_network_when_store_is_injected(self) -> None:
        temporary, root = _library()
        self.addCleanup(temporary.cleanup)
        config = load_r2_config(_settings(), env_file=None)
        result = upload_version(root, ASSET, None, config, store=_MemoryStore())
        receipt = json.loads((root / ASSET / VERSION / "upload.json").read_text(encoding="utf-8"))
        self.assertEqual(result.bucket, "glia-media")
        self.assertEqual(receipt["bucket"], "glia-media")
        self.assertNotIn("secret", json.dumps(receipt))
        self.assertEqual(len(receipt["uploaded"]), 3)


class UploadCommandTest(unittest.TestCase):
    def test_upload_command_stops_before_the_network_when_settings_are_missing(self) -> None:
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("GLIA_R2_") or key.startswith("R2_"):
                del env[key]
        env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "glia_media",
                    "--upload",
                    "--library",
                    temporary,
                    "--asset",
                    ASSET,
                ],
                cwd=temporary,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("HLS 전용 버킷", completed.stderr)
        self.assertNotIn("online-glia", completed.stderr)
