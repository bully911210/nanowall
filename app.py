"""nanowall: add your photos once -> expert review -> 50-wallpaper carousel that rotates itself.

Run:  python app.py   -> http://127.0.0.1:8765   (add --no-browser to skip opening a tab)
Keys live in keys.json next to this file (local only, bound to 127.0.0.1).
"""
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import engine
import nanowall as nw

HOST, PORT = "127.0.0.1", 8765
KEYS_FILE = nw.ROOT / "keys.json"
INDEX = nw.ASSETS / "index.html"
MAX_BODY = 45_000_000  # one base64 photo per request
_keys_lock = threading.Lock()


def load_keys() -> dict:
    if KEYS_FILE.exists():
        try:
            return json.loads(KEYS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    env = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    return {"active": "env", "keys": {"env": env}} if env else {"active": None, "keys": {}}


def active_key() -> str:
    k = load_keys()
    return k["keys"].get(k["active"], "") if k["active"] else ""


def public_keys() -> dict:
    k = load_keys()
    mask = lambda v: f"{v[:4]}…{v[-4:]}" if len(v) > 10 else "••••"
    return {"active": k["active"], "keys": [{"name": n, "hint": mask(v)} for n, v in k["keys"].items()]}


ENGINE = engine.Engine(active_key)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        line, code = str(args[0] if args else ""), str(args[1] if len(args) > 1 else "")
        quiet = ("/api/state", "/photo/", "/face/", "/wall/")
        if code.startswith(("4", "5")) or not any(q in line for q in quiet):
            print(f"[app] {fmt % args}")

    def _send(self, body: bytes, ctype: str, status: int = 200, cache: bool = False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj).encode(), "application/json", status)

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == "/":
                self._send(INDEX.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/state":
                self._json({**engine.state(), "cap": engine.CAROUSEL_CAP, "keys": public_keys()})
            elif url.path.startswith("/photo/"):
                self._send(engine.photo_path(unquote(url.path[7:])).read_bytes(), "image/jpeg", cache=True)
            elif url.path.startswith("/face/"):
                self._send(engine.face_path(unquote(url.path[6:])).read_bytes(), "image/jpeg", cache=True)
            elif url.path.startswith("/wall/"):
                self._send(engine.carousel_path(unquote(url.path[6:])).read_bytes(), "image/jpeg", cache=True)
            else:
                self.send_error(404)
        except nw.NanowallError:
            self.send_error(404)

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
            return self._json({"error": "forbidden origin"}, 403)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json({"error": "file too large"}, 413)
        try:
            b = json.loads(self.rfile.read(length) or b"{}")
            route = urlparse(self.path).path
            if route == "/api/photos/add":
                self._json(engine.add_photo(b["name"], b["data"]))
            elif route == "/api/photos/remove":
                engine.remove_photo(b["id"])
                self._json({"ok": True})
            elif route == "/api/start":
                if not active_key():
                    raise nw.NanowallError("Add your Gemini key first.")
                ENGINE.start()
                self._json({"ok": True})
            elif route == "/api/stop":
                ENGINE.stop()
                self._json({"ok": True})
            elif route == "/api/next":
                self._json({"file": ENGINE.next_wallpaper(int(b.get("step", 1)))})
            elif route == "/api/show":
                ENGINE.show(b["file"])
                self._json({"ok": True})
            elif route == "/api/wall/remove":
                ENGINE.remove(b["file"])
                self._json({"ok": True})
            elif route == "/api/interval":
                engine.update(interval_min=max(1, min(1440, int(b["minutes"]))))
                self._json({"ok": True})
            elif route.startswith("/api/keys/"):
                self._keys(route.rsplit("/", 1)[1], b)
            else:
                self._json({"error": "not found"}, 404)
        except nw.NanowallError as e:
            self._json({"error": str(e)}, 400)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
            self._json({"error": f"bad request: {e}"}, 400)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._json({"error": f"internal error: {type(e).__name__}: {e}"}, 500)

    def _keys(self, action: str, b: dict):
        with _keys_lock:
            data = load_keys()
            name = (b.get("name") or "").strip() or "gemini"
            if action == "add":
                key = (b.get("key") or "").strip()
                if not key:
                    raise nw.NanowallError("Paste a key first.")
                data["keys"][name] = key
                data["active"] = name
            elif action == "use" and name in data["keys"]:
                data["active"] = name
            elif action == "remove":
                data["keys"].pop(name, None)
                if data["active"] == name:
                    data["active"] = next(iter(data["keys"]), None)
            else:
                raise nw.NanowallError("Unknown key action.")
            KEYS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._json(public_keys())


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    print(f"nanowall on {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
