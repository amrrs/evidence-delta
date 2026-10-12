import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from .delta import compare
from .service import Service, load_env, watch_config, ci_gate

WEB = Path(__file__).parent / "web"


def serve(port=8767, service=None):
    load_env()
    service = service or Service()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, data, status=200, content="application/json"):
            raw = (
                data
                if isinstance(data, bytes)
                else json.dumps(data, ensure_ascii=False).encode()
            )
            self.send_response(status)
            self.send_header("Content-Type", content)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path == "/api/history":
                    history = service.store.history()
                    self.send(
                        {
                            "snapshots": [
                                {
                                    k: s[k]
                                    for k in (
                                        "id",
                                        "watch",
                                        "fetched_at",
                                        "mode",
                                        "complete",
                                        "trace",
                                    )
                                }
                                for s in history
                            ],
                            "usage": service.store.usage(),
                            "limit": service.limit,
                            "ttl": service.ttl,
                        }
                    )
                    return
                if path == "/api/verify":
                    self.send(service.store.verify())
                    return
                if path == "/api/export":
                    self.send(service.store.export())
                    return
                if path == "/api/snapshot":
                    self.send(
                        service.store.get(
                            int(parse_qs(urlsplit(self.path).query)["id"][0])
                        )
                    )
                    return
                files = {
                    "/": "index.html",
                    "/app.js": "app.js",
                    "/style.css": "style.css",
                    "/tokens.css": "tokens.css",
                }
                if path not in files:
                    self.send({"error": "Unknown route"}, 404)
                    return
                content = (
                    "text/html; charset=utf-8"
                    if path == "/"
                    else (
                        "text/javascript; charset=utf-8"
                        if path.endswith(".js")
                        else "text/css; charset=utf-8"
                    )
                )
                self.send((WEB / files[path]).read_bytes(), content=content)
            except (ValueError, KeyError):
                self.send({"error": "Invalid or missing snapshot ID"}, 400)

        def do_POST(self):
            origin = self.headers.get("Origin")
            if origin and origin not in (
                f"http://127.0.0.1:{port}",
                f"http://localhost:{port}",
            ):
                self.send({"error": "Cross-origin requests are refused"}, 403)
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.send({"error": "JSON requests are required"}, 415)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size > 16384:
                    raise ValueError("Request exceeds local size limit")
                body = json.loads(self.rfile.read(size))
                if self.path == "/api/capture":
                    watch = watch_config(
                        body.get("query", ""),
                        body.get("engines"),
                        body.get("gl", "in"),
                        body.get("hl", "en"),
                    )
                    result = service.capture(watch, body.get("force") is True)
                elif self.path == "/api/replay":
                    result = service.replay(body.get("id"))
                elif self.path == "/api/compare":
                    result = compare(
                        service.store.get(int(body["before"])),
                        service.store.get(int(body["after"])),
                        body.get("fuzzy", True),
                        float(body.get("p", 0.9)),
                    )
                    result["gate"] = ci_gate(
                        result,
                        int(body.get("max_content", 0)),
                        int(body.get("max_removed", 0)),
                        float(body.get("min_rbo", 0)),
                    )
                else:
                    self.send({"error": "Unknown route"}, 404)
                    return
                self.send(result)
            except (ValueError, KeyError, TypeError) as e:
                self.send({"error": str(e)}, 400)
            except Exception:
                self.send(
                    {"error": "Local operation failed; inspect the server console"}, 500
                )

    print(f"EvidenceDelta workbench: http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
