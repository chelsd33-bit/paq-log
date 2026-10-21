"""테스트 모드용 로컬 모의 사이트 서버 (127.0.0.1 에서만 열림)."""
from __future__ import annotations

import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MOCK_DIR = Path(__file__).resolve().parent.parent / "mock_site"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass


class MockServer:
    def __init__(self, port: int = 0) -> None:
        handler = functools.partial(_QuietHandler, directory=str(MOCK_DIR))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def show_url(self, scenario: str = "normal") -> str:
        return f"{self.base_url}/show.html?scenario={scenario}"

    def start(self) -> "MockServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
