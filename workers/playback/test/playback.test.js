import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import worker from "../src/index.js";
import { authorizePath } from "../src/grant.js";

const ASSET = "a".repeat(32);
const VERSION = "b".repeat(32);
const SECRET = "test-lms-token";
const NOW = Date.parse("2026-11-20T00:00:00Z");
const REPO = new URL("../../../", import.meta.url);

async function issueGrant(at = "datetime(2026, 11, 20, tzinfo=timezone.utc)") {
  const root = await mkdtemp(join(tmpdir(), "glia-playback-"));
  const version = join(root, ASSET, VERSION);
  await mkdir(version, { recursive: true });
  const playlist = `vod/packages/${ASSET}/${VERSION}/720p/index.m3u8`;
  const segment = `vod/packages/${ASSET}/${VERSION}/720p/segment-000.ts`;
  await writeFile(join(version, "manifest.json"), JSON.stringify({
    inspection: { passed: true },
    storage: { playlist_key: playlist, segment_keys: [segment] },
  }));
  await writeFile(join(version, "upload.json"), JSON.stringify({
    uploaded: [playlist, segment],
    already_present: [],
  }));
  const script = `
from datetime import datetime, timezone
from pathlib import Path
from glia_media.playback import issue_grant
print(issue_grant(Path(${JSON.stringify(root)}), ${JSON.stringify(ASSET)}, ${JSON.stringify(VERSION)}, ${JSON.stringify(SECRET)}, ${at})["grant"])
`;
  const grant = execFileSync("python3", ["-c", script], {
    cwd: REPO,
    env: { ...process.env, PYTHONPATH: "src" },
    encoding: "utf8",
  }).trim();
  return grant;
}

test("accepts a grant issued by the python service for the playlist and a segment", async () => {
  const grant = await issueGrant();
  const playlist = `/p/${grant}/vod/packages/${ASSET}/${VERSION}/720p/index.m3u8`;
  const segment = `/p/${grant}/vod/packages/${ASSET}/${VERSION}/720p/segment-000.ts`;
  assert.equal((await authorizePath(playlist, SECRET, NOW)).ok, true);
  assert.equal(
    (await authorizePath(segment, SECRET, NOW)).key,
    `vod/packages/${ASSET}/${VERSION}/720p/segment-000.ts`,
  );
  assert.equal((await authorizePath(playlist.replace("index.m3u8", "source.mp4"), SECRET, NOW)).status, 403);
  assert.equal((await authorizePath(playlist.replace("index.m3u8", "segment-00000.ts"), SECRET, NOW)).ok, true);
  assert.equal((await authorizePath(playlist.replace("index.m3u8", "segment-00.ts"), SECRET, NOW)).status, 403);
  assert.equal((await authorizePath(playlist, SECRET, NOW + 11 * 60 * 1000)).status, 401);
  const tampered = `${grant.slice(0, -1)}${grant.endsWith("A") ? "B" : "A"}`;
  assert.equal((await authorizePath(playlist.replace(grant, tampered), SECRET, NOW)).status, 401);
  assert.equal((await authorizePath(playlist.replace(`/p/${grant}/vod/`, "/p/grant/media/"), SECRET, NOW)).status, 404);
});

test("reads only the authorized object from the private bucket", async () => {
  const grant = await issueGrant("datetime.now(timezone.utc)");
  const seen = [];
  const env = {
    PLAYBACK_SECRET: SECRET,
    MEDIA: {
      async get(key) {
        seen.push(key);
        return { body: "bytes", size: 8, httpEtag: "\"etag\"" };
      },
    },
  };
  const response = await worker.fetch(
    new Request(`https://media.example/p/${grant}/vod/packages/${ASSET}/${VERSION}/720p/segment-000.ts`),
    env,
  );
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "bytes");
  assert.deepEqual(seen, [`vod/packages/${ASSET}/${VERSION}/720p/segment-000.ts`]);
  assert.match(response.headers.get("cache-control"), /^private, max-age=\d+$/);
  assert.equal(response.headers.get("content-type"), "video/mp2t");
  assert.equal(response.headers.get("access-control-allow-headers"), "range");
  const preflight = await worker.fetch(
    new Request(`https://media.example/p/${grant}/vod/packages/${ASSET}/${VERSION}/720p/index.m3u8`, { method: "OPTIONS" }),
    env,
  );
  assert.equal(preflight.status, 204);
  assert.equal(preflight.headers.get("access-control-allow-headers"), "range");

  const ranged = [];
  env.MEDIA.get = async (key, options) => {
    ranged.push(options);
    return {
      body: "ab",
      size: 8,
      range: options ? { offset: 0, length: 2 } : { offset: 0, length: 8 },
      httpEtag: "\"etag\"",
    };
  };
  const full = await worker.fetch(
    new Request(`https://media.example/p/${grant}/vod/packages/${ASSET}/${VERSION}/720p/segment-000.ts`),
    env,
  );
  const partial = await worker.fetch(
    new Request(`https://media.example/p/${grant}/vod/packages/${ASSET}/${VERSION}/720p/segment-000.ts`, {
      headers: { range: "bytes=0-1" },
    }),
    env,
  );
  assert.equal(full.status, 200);
  assert.equal(ranged[0], undefined);
  assert.equal(partial.status, 206);
  assert.equal(partial.headers.get("content-range"), "bytes 0-1/8");
});
