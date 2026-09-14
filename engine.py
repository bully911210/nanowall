"""nanowall engine: photos -> people index -> identity pack -> LOCK render -> STYLE from lock -> watermark -> carousel.

Worker order each tick: detect new photos, regroup identities when the album changed, then fill the carousel
slot the recognisability quota needs most (20 LOCK / 15 LIGHT / 10 FACED / 5 WILDCARD).
"""
import base64
import io
import json
import random
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

import identity as idn
import modes
import nanowall as nw

LIB_DIR = nw.ROOT / "library"
LOCK_DIR = LIB_DIR / "locks"
CAROUSEL_DIR = nw.ROOT / "carousel"
STATE_FILE = nw.ROOT / "carousel.json"
CAROUSEL_CAP = sum(modes.QUOTA.values())
MAX_UPLOAD_BYTES = 30_000_000
LOCK_ATTEMPTS = 2   # if LOCK fails twice the pack or the face size is the problem, not the prompt
STYLE_ATTEMPTS = 2
WATERMARK = "follow on x.com @franzsalessense"
FONT_PATHS = [r"C:\Windows\Fonts\segoeuisb.ttf", r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"]

_lock = threading.RLock()


# ---------- state ----------

def _load() -> dict:
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            s.setdefault("identities", [])
            s.setdefault("index_sig", "")
            stale = [p for p in s.get("photos", []) if p.get("detect") and p["detect"].get("version") != idn.DETECT_VERSION]
            if stale:  # detection improved: re-index those photos and regroup the album
                for p in stale:
                    p["detect"], p["locks"], p["lock_fails"] = None, [], 0
                s["identities"], s["index_sig"] = [], ""
            for p in s.get("photos", []):  # upgrade photos saved by the pre-identity version
                p.pop("review", None)
                p.pop("next_dir", None)
                p.setdefault("detect", None)
                p.setdefault("locks", [])
                p.setdefault("lock_fails", 0)
                p.setdefault("styles_used", [])
                if not isinstance(p.get("made"), dict):
                    p["made"] = {m: 0 for m in modes.QUOTA}
            return s
        except json.JSONDecodeError:
            pass
    return {"photos": [], "identities": [], "index_sig": "", "carousel": [], "interval_min": 15,
            "running": False, "cursor": 0, "status": "idle", "last_error": ""}


def _save(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def state() -> dict:
    with _lock:
        return _load()


def update(**changes) -> dict:
    with _lock:
        s = _load()
        s.update(changes)
        _save(s)
        return s


def _patch_photo(pid: str, fn) -> None:
    with _lock:
        s = _load()
        for p in s["photos"]:
            if p["id"] == pid:
                fn(p)
        _save(s)


# ---------- onboarding ----------

def add_photo(name: str, data_b64: str) -> dict:
    raw = base64.b64decode(data_b64)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise nw.NanowallError(f"{name}: larger than 30MB.")
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")  # re-encode: strips EXIF/GPS
            im.thumbnail((3000, 3000))                       # keep resolution: small faces drift
            LIB_DIR.mkdir(exist_ok=True)
            pid = uuid.uuid4().hex[:12]
            im.save(LIB_DIR / f"{pid}.jpg", "JPEG", quality=94)
    except Exception as e:
        raise nw.NanowallError(f"{name}: not a readable image ({e}).")
    photo = {"id": pid, "name": Path(name).name, "detect": None, "locks": [], "lock_fails": 0,
             "made": {m: 0 for m in modes.QUOTA}, "styles_used": []}
    with _lock:
        s = _load()
        s["photos"].append(photo)
        _save(s)
    return photo


def remove_photo(pid: str) -> None:
    with _lock:
        s = _load()
        s["photos"] = [p for p in s["photos"] if p["id"] != pid]
        _save(s)
    (LIB_DIR / f"{Path(pid).name}.jpg").unlink(missing_ok=True)


def photo_path(pid: str) -> Path:
    path = LIB_DIR / f"{Path(pid).name}.jpg"
    if not path.exists():
        raise nw.NanowallError("Unknown photo.")
    return path


def face_path(name: str) -> Path:
    path = idn.FACES_DIR / Path(name).name
    if not path.exists():
        raise nw.NanowallError("Unknown face crop.")
    return path


# ---------- delivery ----------

def watermark(img: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(img)).convert("RGB")
    w, h = im.size
    size = max(14, int(h * 0.016))
    font = next((ImageFont.truetype(f, size) for f in FONT_PATHS if Path(f).exists()), ImageFont.load_default())
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    left, top, right, bottom = draw.textbbox((0, 0), WATERMARK, font=font)
    margin = int(h * 0.022)
    taskbar = int(h * 0.055)  # the Windows taskbar covers ~5% of the bottom edge; sit just above it
    x, y = w - (right - left) - margin, h - (bottom - top) - taskbar - margin - top
    draw.text((x + 1, y + 1), WATERMARK, font=font, fill=(0, 0, 0, 110))
    draw.text((x, y), WATERMARK, font=font, fill=(255, 255, 255, 170))
    return Image.alpha_composite(im.convert("RGBA"), layer).convert("RGB")


def carousel_path(fid: str) -> Path:
    path = CAROUSEL_DIR / Path(fid).name
    if not path.exists():
        raise nw.NanowallError("Unknown wallpaper.")
    return path


def _deliver(img: bytes, photo: dict, mode: str, label: str, verdict: dict) -> dict:
    CAROUSEL_DIR.mkdir(exist_ok=True)
    fid = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.jpg"
    watermark(img).save(CAROUSEL_DIR / fid, "JPEG", quality=94)
    caption = "colour of this day" if mode == "WILDCARD" else label
    return {"file": fid, "photo": photo["id"], "mode": mode, "style": caption,
            "score": verdict.get("recognisable"), "at": datetime.now().isoformat(timespec="seconds")}


# ---------- generation ----------

def _open(path: Path) -> Image.Image:
    with Image.open(path) as im:
        return im.convert("RGB")


def _attempts(build_parts, pack, mode, api_key, attempts, style="photorealistic photograph"):
    """Render, judge against the real face crops, retry once with the judge's fix."""
    best, fix = None, ""
    for _ in range(attempts):
        parts = build_parts()
        if fix:
            parts[-1]["text"] += f"\n\nFix from review: {fix}"
        try:
            img = modes.render(parts, api_key)
        except nw.SafetyBlocked as e:
            if best is None:
                best = (None, {"passed": False, "recognisable": 0, "safety": True, "fix": str(e)})
            continue
        verdict = modes.judge(img, pack, mode, api_key, style)
        if best is None or best[0] is None or verdict.get("recognisable", 0) > best[1].get("recognisable", 0):
            best = (img, verdict)
        if verdict["passed"]:
            return img, verdict
        fix = verdict.get("fix", "")
    return best[0], best[1]


def make_lock(photo: dict, identities: list, api_key: str):
    pack = idn.pack_for(photo, identities)
    change = modes.pick_change()
    source = _open(photo_path(photo["id"]))
    img, verdict = _attempts(lambda: modes.lock_request(source, pack, change), pack, "LOCK", api_key, LOCK_ATTEMPTS)
    return img, verdict, change


def make_style(photo: dict, identities: list, mode: str, api_key: str):
    pack = idn.pack_for(photo, identities)
    lock_img = _open(LOCK_DIR / random.choice(photo["locks"]))
    style = modes.pick_style(mode, photo["styles_used"])
    img, verdict = _attempts(lambda: modes.style_request(lock_img, pack, style, mode), pack, mode, api_key, STYLE_ATTEMPTS, style)
    return img, verdict, style


def _eligible(photos: list, mode: str) -> list:
    ready = [p for p in photos if p["detect"] and p["detect"]["score"] >= 4
             and p.get("safety_blocks", 0) < modes.SAFETY_SKIP_LIMIT]
    if mode == "LOCK":
        return [p for p in ready if p["lock_fails"] < LOCK_ATTEMPTS * 2]
    faced = mode in ("LIGHT", "FACED")
    return [p for p in ready if p["locks"] and not (faced and idn.people_count(p) > idn.MAX_PEOPLE_FOR_STYLE)]


# ---------- background worker + rotator ----------

class Engine:
    def __init__(self, key_provider):
        self.key_provider = key_provider
        self._wake = threading.Event()
        threading.Thread(target=self._work_loop, daemon=True).start()
        threading.Thread(target=self._rotate_loop, daemon=True).start()

    def start(self):
        update(running=True, last_error="")
        self._wake.set()

    def stop(self):
        update(running=False, status="paused")

    def _tick(self, key: str) -> None:
        s = state()
        pending = next((p for p in s["photos"] if not p["detect"]), None)
        if pending:  # Stage A: people index for each new photo
            update(status=f"indexing faces in {pending['name']}")
            try:
                result = idn.detect(photo_path(pending["id"]), pending["id"], key)
            except nw.NanowallError as e:
                if "error " in str(e) and not str(e).startswith("Detection failed"):
                    raise  # real API/network outage: let the worker back off and retry
                # filtered or unreadable response: park this photo and carry on with the rest
                result = {"version": idn.DETECT_VERSION, "subjects": [], "score": 0, "theme": "skipped",
                          "why": "", "critique": "", "risks": f"Gemini would not analyse this photo ({e})."[:200]}
                update(last_error=f"Skipped {pending['name']}: Gemini would not analyse it. Continuing.")
            _patch_photo(pending["id"], lambda p: p.update(detect=result))
            return
        sig = ",".join(sorted(p["id"] for p in s["photos"]))
        if s["photos"] and sig != s["index_sig"]:  # album changed: regroup faces into identities
            update(status="grouping faces into people")
            update(identities=idn.cluster(s["photos"], key), index_sig=sig)
            return

        mode = modes.next_mode(s["carousel"])
        candidates = _eligible(s["photos"], mode)
        if not candidates:  # styles need a lock first: make one
            mode, candidates = "LOCK", _eligible(s["photos"], "LOCK")
        if not candidates:
            update(running=False, status="no usable photos: add clearer photos with larger faces")
            return
        photo = min(candidates, key=lambda p: (p["made"][mode], -p["detect"]["score"]))
        n = len(s["carousel"]) + 1
        update(status=f"{mode} · {photo['name']} · {n}/{CAROUSEL_CAP}")

        if mode == "LOCK":
            img, verdict, label = make_lock(photo, s["identities"], key)
        else:
            img, verdict, label = make_style(photo, s["identities"], mode, key)

        if verdict.get("safety"):  # Gemini filtered every attempt: skip, note it, move straight on
            def blocked(p):
                p["safety_blocks"] = p.get("safety_blocks", 0) + 1
                if mode != "LOCK":
                    p["styles_used"].append(label)
            _patch_photo(photo["id"], blocked)
            update(last_error=f"Skipped {photo['name']} {mode}: {verdict['fix']}. Continuing."[:400])
            return

        if not verdict["passed"]:
            def failed(p):
                if mode == "LOCK":
                    p["lock_fails"] += 1
                else:
                    p["styles_used"].append(label)  # don't retry the same failing style next time
            _patch_photo(photo["id"], failed)
            update(last_error=f"{photo['name']} {mode} rejected: recognisable {verdict.get('recognisable', '?')}/10. {verdict.get('fix', '')}"[:400])
            return

        item = _deliver(img, photo, mode, label, verdict)

        def passed(p):
            p["made"][mode] += 1
            if mode == "LOCK":
                LOCK_DIR.mkdir(parents=True, exist_ok=True)
                lock_file = f"{p['id']}_{len(p['locks'])}.png"
                (LOCK_DIR / lock_file).write_bytes(img)  # clean, unwatermarked: the source for STYLE passes
                p["locks"].append(lock_file)
            else:
                p["styles_used"].append(label)
        _patch_photo(photo["id"], passed)
        with _lock:
            st = _load()
            st["carousel"].append(item)
            st["cursor"] = len(st["carousel"]) - 1
            st["last_error"] = ""
            _save(st)
        nw.set_wallpaper(CAROUSEL_DIR / item["file"])

    def _work_loop(self):
        while True:
            s = state()
            if not s["running"] or len(s["carousel"]) >= CAROUSEL_CAP:
                if s["running"]:
                    update(running=False, status=f"carousel full ({CAROUSEL_CAP}), rotating")
                self._wake.wait(5)
                self._wake.clear()
                continue
            key = self.key_provider()
            try:
                if not key:
                    raise nw.NanowallError("No Gemini API key.")
                self._tick(key)
            except nw.NanowallError as e:
                msg = str(e)
                fatal = any(k in msg for k in ("No Gemini API key", "API key not valid", "PERMISSION_DENIED", "billing"))
                update(last_error=msg[:400], status="stopped" if fatal else "retrying in 60s", running=not fatal)
                if not fatal:
                    time.sleep(60)
            except Exception as e:  # keep the worker alive, surface the problem
                import traceback
                traceback.print_exc()
                update(last_error=f"{type(e).__name__}: {e}"[:400], status="retrying in 60s")
                time.sleep(60)

    def _rotate_loop(self):
        last = time.time()
        while True:
            time.sleep(10)
            s = state()
            if not s["carousel"] or time.time() - last < s["interval_min"] * 60:
                continue
            try:
                self.next_wallpaper()
            except nw.NanowallError:
                pass
            last = time.time()

    def next_wallpaper(self, step: int = 1):
        with _lock:
            s = _load()
            if not s["carousel"]:
                raise nw.NanowallError("Carousel is empty.")
            s["cursor"] = (s["cursor"] + step) % len(s["carousel"])
            _save(s)
            fid = s["carousel"][s["cursor"]]["file"]
        nw.set_wallpaper(CAROUSEL_DIR / fid)
        return fid

    def show(self, fid: str):
        with _lock:
            s = _load()
            idx = next((i for i, c in enumerate(s["carousel"]) if c["file"] == fid), None)
            if idx is None:
                raise nw.NanowallError("Unknown wallpaper.")
            s["cursor"] = idx
            _save(s)
        nw.set_wallpaper(carousel_path(fid))

    def remove(self, fid: str):
        with _lock:
            s = _load()
            s["carousel"] = [c for c in s["carousel"] if c["file"] != fid]
            s["cursor"] = min(s["cursor"], max(0, len(s["carousel"]) - 1))
            _save(s)
        (CAROUSEL_DIR / Path(fid).name).unlink(missing_ok=True)
        self._wake.set()
