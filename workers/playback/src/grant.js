const ID = /^[0-9a-f]{32}$/;
const SEGMENT = /^segment-\d{3,5}\.ts$/;

export async function authorizePath(pathname, secret, nowMs) {
  if (!secret) return { ok: false, status: 503 };
  const parts = pathname.split("/");
  if (parts.length !== 9 || parts[0] !== "" || parts[1] !== "p" || parts[3] !== "vod" || parts[4] !== "packages" || parts[7] !== "720p") {
    return { ok: false, status: 404 };
  }
  const [, , grant, , , assetId, versionId, , name] = parts;
  if (!ID.test(assetId) || !ID.test(versionId) || parts.slice(1).some((part) => part === "" || part === "." || part === "..")) {
    return { ok: false, status: 404 };
  }
  if (name !== "index.m3u8" && !SEGMENT.test(name)) {
    return { ok: false, status: 403 };
  }
  const payload = await openGrant(grant, secret, nowMs);
  if (!payload.ok) return payload;
  if (payload.assetId !== assetId || payload.versionId !== versionId) {
    return { ok: false, status: 403 };
  }
  const maxAge = Math.max(0, Math.floor(payload.exp - nowMs / 1000));
  return {
    ok: true,
    key: `vod/packages/${assetId}/${versionId}/720p/${name}`,
    name,
    maxAge,
  };
}

async function openGrant(grant, secret, nowMs) {
  const dot = grant.indexOf(".");
  if (dot <= 0 || dot !== grant.lastIndexOf(".")) return { ok: false, status: 401 };
  const body = grant.slice(0, dot);
  const signature = grant.slice(dot + 1);
  const expected = await sign(secret, body);
  if (expected.length !== signature.length || !timingSafeEqual(expected, signature)) {
    return { ok: false, status: 401 };
  }
  let payload;
  try {
    payload = JSON.parse(new TextDecoder().decode(decodeBase64Url(body)));
  } catch {
    return { ok: false, status: 401 };
  }
  if (!payload || typeof payload.exp !== "number" || nowMs >= payload.exp * 1000) {
    return { ok: false, status: 401 };
  }
  if (!ID.test(payload.asset_id) || !ID.test(payload.version_id)) {
    return { ok: false, status: 401 };
  }
  return { ok: true, assetId: payload.asset_id, versionId: payload.version_id, exp: payload.exp };
}

let signingSecret = "";
let signingKey = null;

async function sign(secret, body) {
  if (signingKey === null || signingSecret !== secret) {
    signingKey = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
    signingSecret = secret;
  }
  const mac = await crypto.subtle.sign("HMAC", signingKey, new TextEncoder().encode(body));
  return encodeBase64Url(new Uint8Array(mac));
}

function encodeBase64Url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

function decodeBase64Url(value) {
  const padded = value.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - (value.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function timingSafeEqual(left, right) {
  let mismatch = left.length ^ right.length;
  for (let index = 0; index < left.length; index += 1) {
    mismatch |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return mismatch === 0;
}
