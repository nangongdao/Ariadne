"""视觉/交互验证用 mock 服务器：静态托管 dist + 假 /v1/models API。

仅供本地验收（浏览器程序化检查 Toast/命令面板/动效），不属于产品代码。
用法：python scripts/mock_api_for_review.py [port]
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIST = Path(__file__).resolve().parent.parent / "dist"

MODELS = [
    {
        "id": "m1",
        "name": "工作用 GPT-4o",
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_prefix": "sk-proj-AB12",
        "base_url": "",
        "degraded_model": "gpt-4o-mini",
        "is_default": True,
        "is_active": True,
        "sort_order": 0,
        "last_used_at": None,
        "created_at": "2026-08-28T10:00:00Z",
    },
    {
        "id": "m2",
        "name": "Claude Sonnet",
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "api_key_prefix": "sk-ant-XYZ9",
        "base_url": "https://api.anthropic.com",
        "degraded_model": "",
        "is_default": False,
        "is_active": True,
        "sort_order": 1,
        "last_used_at": None,
        "created_at": "2026-08-28T10:01:00Z",
    },
]


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/v1/models":
            self._send(200, {"models": MODELS, "cryptography_available": True})
        elif path == "/v1/models/default":
            default = next((m for m in MODELS if m["is_default"]), None)
            if default is None:
                self._send(404, {"found": False})
            else:
                self._send(200, {**default, "api_key": "sk-mock", "found": True})
        else:
            self._static(path)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        item = {
            "id": f"m{len(MODELS) + 1}",
            "api_key_prefix": (data.get("api_key") or "")[:12],
            "is_active": True,
            "sort_order": len(MODELS),
            "last_used_at": None,
            "created_at": "2026-08-28T12:00:00Z",
            **{k: v for k, v in data.items() if k != "api_key"},
        }
        MODELS.append(item)
        self._send(201, {**item, "api_key": data.get("api_key", "")})

    def do_PUT(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        item = next((m for m in MODELS if m["id"] == path.rstrip("/").split("/")[-1]), None)
        if item is None:
            self._send(404, {"detail": "not found"})
            return
        item.update(data)
        self._send(200, item)

    def do_DELETE(self):
        path = self.path.split("?")[0]
        item_id = path.rstrip("/").split("/")[-1]
        MODELS[:] = [m for m in MODELS if m["id"] != item_id]
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _static(self, path):
        file = DIST / (path.lstrip("/") or "index.html")
        if not file.exists():
            file = DIST / "index.html"  # SPA fallback
        ctype = {
            ".html": "text/html",
            ".js": "text/javascript",
            ".css": "text/css",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".woff2": "font/woff2",
        }.get(file.suffix, "application/octet-stream")
        body = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4173
    print(f"mock review server on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
