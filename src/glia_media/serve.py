"""LMS가 접속하는 로컬 API 서버."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections.abc import Callable
from pathlib import Path

from glia_media.lms import LmsError, handle_request


def serve(
    library: Path,
    token: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    ready: Callable[[int], None] | None = None,
    playback_base: str = "",
) -> None:
    library = library.expanduser().resolve()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._respond()

        def do_POST(self) -> None:
            self._respond()

        def log_message(self, format: str, *args: object) -> None:
            return

        def _respond(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                length = -1
            if length < 0 or length > 64 * 1024:
                status, payload = 413, {"error": "요청이 너무 큽니다."}
            else:
                body = self.rfile.read(length) if length else b""
                try:
                    status, payload = handle_request(
                        library,
                        self.command,
                        self.path.split("?", 1)[0],
                        self.headers.get("Authorization", ""),
                        body,
                        token,
                        playback_base=playback_base,
                    )
                except LmsError as error:
                    status = error.status
                    payload = {"error": error.summary}
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer((host, port), Handler)
    if ready is not None:
        ready(int(server.server_address[1]))
    try:
        server.serve_forever()
    finally:
        server.server_close()
