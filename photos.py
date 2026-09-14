"""Find the user's own meaningful photos (camera shots, kept pictures) to use as taste + reference material.

Local only: scanning and thumbnailing never leave the machine. Only photos the user approves
in the library are ever sent to Gemini.
"""
import io
import json
import os
import random
from pathlib import Path

from PIL import Image, ImageOps

import nanowall as nw

Image.MAX_IMAGE_PIXELS = 200_000_000  # big camera panoramas are fine
HOME = Path.home()
ROOTS = [HOME / "Pictures", HOME / "OneDrive" / "Pictures", HOME / "Desktop", HOME / "Downloads"]
EXTS = {".jpg", ".jpeg", ".png", ".webp"}
SKIP_PARTS = ("screenshot", "screen shot", "capture", "sticker", "icon", "logo", "node_modules",
              "appdata", "cache", "thumb", "wallpapers", "whatsapp", "scan", "invoice", "receipt", "id ")
LIBRARY_FILE = nw.ROOT / "library.json"
MAX_FILES = 25000
MIN_SIDE = 1000


def _score(path: Path, stat) -> float:
    """Camera photos in Pictures, big and not screenshots/docs, score highest."""
    try:
        with Image.open(path) as im:
            w, h = im.size
            exif = im.getexif()
    except Exception:
        return -1
    if max(w, h) < MIN_SIDE:
        return -1
    score = min(max(w, h) / 4000, 1.0)
    if exif.get(271) or exif.get(272):  # camera Make/Model -> a real photo someone took
        score += 5
    if "Pictures" in path.parts:
        score += 1
    if path.suffix.lower() == ".png" and not exif:
        score -= 0.8  # PNG without EXIF is usually a graphic, export or screenshot
    return score


def scan(limit: int = 36) -> list:
    seen = 0
    found = []
    for root in ROOTS:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and not any(s in d.lower() for s in SKIP_PARTS)]
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix.lower() not in EXTS or any(s in name.lower() for s in SKIP_PARTS):
                    continue
                seen += 1
                if seen > MAX_FILES:
                    break
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_size < 150_000:
                    continue
                found.append((p, st))
    random.shuffle(found)
    # every JPEG is checked (that's where camera photos live); PNG/WebP are sampled to keep scans fast
    jpgs = [x for x in found if x[0].suffix.lower() in (".jpg", ".jpeg")]
    others = [x for x in found if x[0].suffix.lower() not in (".jpg", ".jpeg")][:800]
    scored = []
    for p, st in jpgs + others:
        s = _score(p, st)
        if s > 0:
            scored.append((s + random.random() * 0.5, p))
    scored.sort(reverse=True)
    # spread across folders so one holiday album doesn't dominate
    picked, per_folder = [], {}
    for _, p in scored:
        if per_folder.get(p.parent, 0) >= 3:
            continue
        per_folder[p.parent] = per_folder.get(p.parent, 0) + 1
        picked.append(str(p))
        if len(picked) >= limit:
            break
    return picked


def jpeg_bytes(path: str, side: int = 768) -> bytes:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((side, side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()


def load_library() -> dict:
    if LIBRARY_FILE.exists():
        try:
            return json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"approved": [], "candidates": [], "used": {}}


def save_library(lib: dict) -> None:
    LIBRARY_FILE.write_text(json.dumps(lib, indent=2), encoding="utf-8")


def pick_refs(lib: dict, k: int) -> list:
    """Least-used approved photos first, so every run draws on different memories."""
    approved = [p for p in lib["approved"] if Path(p).exists()]
    if not approved:
        return []
    random.shuffle(approved)
    approved.sort(key=lambda p: lib["used"].get(p, 0))
    return approved[:k]

