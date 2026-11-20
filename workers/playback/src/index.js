import { authorizePath } from "./grant.js";

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return deny(405);
    }
    const decision = await authorizePath(new URL(request.url).pathname, env.PLAYBACK_SECRET, Date.now());
    if (!decision.ok) return deny(decision.status);
    if (!env.MEDIA) return deny(503);

    if (request.method === "HEAD") {
      const head = await env.MEDIA.head(decision.key);
      if (!head) return deny(404);
      return new Response(null, { status: 200, headers: objectHeaders(head, decision.name, decision.maxAge) });
    }

    const requestedRange = request.headers.get("range");
    const object = await env.MEDIA.get(decision.key, requestedRange ? { range: request.headers } : undefined);
    if (!object) return deny(404);
    const headers = objectHeaders(object, decision.name, decision.maxAge);
    if (requestedRange && object.range) {
      const end = object.range.offset + object.range.length - 1;
      headers.set("content-range", `bytes ${object.range.offset}-${end}/${object.size}`);
      headers.set("content-length", String(object.range.length));
    }
    return new Response(object.body, { status: requestedRange && object.range ? 206 : 200, headers });
  },
};

function objectHeaders(object, name, maxAge) {
  const headers = corsHeaders();
  headers.set("content-type", name.endsWith(".m3u8") ? "application/vnd.apple.mpegurl" : "video/mp2t");
  headers.set("cache-control", `private, max-age=${maxAge}`);
  headers.set("accept-ranges", "bytes");
  if (object.httpEtag) headers.set("etag", object.httpEtag);
  if (!object.range && Number.isFinite(object.size)) headers.set("content-length", String(object.size));
  return headers;
}

function corsHeaders() {
  const headers = new Headers();
  headers.set("access-control-allow-origin", "*");
  headers.set("access-control-allow-methods", "GET, HEAD, OPTIONS");
  headers.set("access-control-allow-headers", "range");
  headers.set("access-control-max-age", "86400");
  headers.set("access-control-expose-headers", "Content-Length, Content-Range, Accept-Ranges");
  return headers;
}

function deny(status) {
  const headers = corsHeaders();
  headers.set("cache-control", "no-store");
  return new Response(null, { status, headers });
}
