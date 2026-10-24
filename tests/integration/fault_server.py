"""A real local HTTP server speaking the Ollama API (/api/chat, /api/generate) with scripted faults.

Requests for a model with a script get the next scripted behavior (the last one repeats):
"429" (Retry-After: 1), "500", "hang" (holds the socket past any client timeout), "ok" (valid).
Requests for any other model are proxied to the real Ollama, so one api_base serves a faulty
primary and a real fallback.
"""

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HANG_S = 6.0


class FaultServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, upstream: str) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.upstream = upstream
        self.scripts: dict[str, list[str]] = {}
        self.hits: list[tuple[str, str]] = []  # (path, model) of every request received
        self._lock = threading.Lock()
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def hit_count(self, model: str) -> int:
        return sum(1 for _, m in self.hits if m == model)

    def next_behavior(self, path: str, model: str) -> str | None:
        with self._lock:
            self.hits.append((path, model))
            script = self.scripts.get(model)
            if not script:
                return None
            return script.pop(0) if len(script) > 1 else script[0]


class _Handler(BaseHTTPRequestHandler):
    server: FaultServer

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(self, code: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        model = json.loads(raw or b"{}").get("model", "")
        behavior = self.server.next_behavior(self.path, model)
        if behavior is None:
            req = urllib.request.Request(
                self.server.upstream + self.path, raw, {"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    self._send(r.status, r.read())
            except urllib.error.HTTPError as e:
                self._send(e.code, e.read())
        elif behavior == "429":
            self._send(429, b'{"error":"slow down"}', {"Retry-After": "1"})
        elif behavior == "500":
            self._send(500, b'{"error":"boom"}')
        elif behavior == "hang":
            time.sleep(HANG_S)
        else:
            counts = {"prompt_eval_count": 5, "eval_count": 3, "done": True, "model": model}
            reply = (
                {"message": {"role": "assistant", "content": "scripted ok"}}
                if self.path == "/api/chat"
                else {"response": "scripted ok"}
            )
            self._send(200, json.dumps({**reply, **counts}).encode())
