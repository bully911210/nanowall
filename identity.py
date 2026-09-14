"""Stage A: people index. Detect every person/dog per photo, crop faces, group crops into album identities,
and build a small identity pack per render (source moment + one face-pack image per identity, 4-5 refs max).
"""
import base64
import io
import json
from pathlib import Path

from PIL import Image, ImageOps, ImageStat

import auto
import nanowall as nw

FACES_DIR = nw.ROOT / "library" / "faces"
MAX_IDENTITIES_PER_RENDER = 3   # + source image + optional dog = 4-5 refs, the useful band
MAX_PEOPLE_FOR_STYLE = 4        # bigger groups blow the consistency budget: LOCK only

DETECT_VERSION = 4
MIN_FACE_SHARE = 0.0025   # smaller faces are background people or unusable for likeness
MIN_CROP_STDDEV = 14      # flat/black crops mean the box missed

DETECT_BRIEF = """You are a portrait photographer preparing a photo for identity-preserving image generation.
Find the MAIN SUBJECTS only: the people and dogs/cats the photo is about. IGNORE background people,
passers-by, crowds, blurry or tiny figures, and anyone whose face is not clearly visible.
Return ONLY JSON, no fences:
{"subjects": [{"kind": "person|dog|cat", "label": "<short role, e.g. 'girl ~8', 'dad', 'baby', 'brown dog'>",
   "box_2d": [ymin, xmin, ymax, xmax],   // tight box around the FACE only (animal: head), normalised 0-1000
   "face_quality": <1-10: large in frame, eyes visible, sharp, no grimace/powder/props covering face, even light>}],
 "score": <1-10 strength as wallpaper source>,
 "theme": "<2-5 word core theme>",
 "why": "<1 sentence: why this moment matters>",
 "critique": "<1-2 sentences, photographer's eye>",
 "risks": "<empty, or what will make faces drift: faces small, harsh noon light, face paint, toy glasses, motion blur>"}
Skip people whose face is not visible at all."""

CLUSTER_BRIEF = """These are {n} face crops from one family album, numbered 0..{last} in order.
Group crops that show the SAME individual (person or animal). Be strict: siblings are different people.
For each identity, choose up to 3 best crops: face large, eyes visible, least blur/grimace/face paint,
include a three-quarter view if one exists.
Return ONLY JSON: {{"identities": [{{"name": "<role, e.g. 'girl with glasses'>", "kind": "person|dog|cat",
"members": [indices], "best": [up to 3 indices, best first]}}]}}"""


def _img64(im: Image.Image, side: int, quality: int = 88) -> str:
    im = im.convert("RGB")
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def part(im: Image.Image, side: int = 1024) -> dict:
    return {"inlineData": {"mimeType": "image/jpeg", "data": _img64(im, side)}}


def _json(data: dict) -> dict:
    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw[raw.find("{"):raw.rfind("}") + 1])


def _cascade():
    import sys
    import cv2
    base = Path(getattr(sys, "_MEIPASS", "")) / "cv2data" if getattr(sys, "frozen", False) else Path(cv2.data.haarcascades)
    return cv2, cv2.CascadeClassifier(str(base / "haarcascade_frontalface_default.xml"))


def _iou(a: tuple, b: tuple) -> float:
    ax1, ay1, bx1, by1 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw, ih = max(0, min(ax1, bx1) - max(a[0], b[0])), max(0, min(ay1, by1) - max(a[1], b[1]))
    inter = iw * ih
    return inter / (a[2] * a[3] + b[2] * b[3] - inter) if inter else 0.0


def _snap_face(im: Image.Image, x: float, y: float, bw: float, bh: float, taken: list) -> tuple:
    """Search a 2.5x region around the model's box with OpenCV; recentre on the real face NEAREST the box
    that no other subject has claimed. Two subjects must never collapse onto the same face."""
    try:
        cv2, cascade = _cascade()
        import numpy as np
    except Exception as e:  # detector unavailable: keep the model box
        print(f"[identity] face snap unavailable: {e}")
        return x, y, bw, bh
    w, h = im.size
    cx, cy, side = x + bw / 2, y + bh / 2, max(bw, bh) * 2.5
    rx0, ry0 = int(max(0, cx - side)), int(max(0, cy - side))
    rx1, ry1 = int(min(w, cx + side)), int(min(h, cy + side))
    region = np.array(im.crop((rx0, ry0, rx1, ry1)).convert("L"))
    min_side = max(24, int(min(bw, bh) * 0.4))
    faces = cascade.detectMultiScale(region, scaleFactor=1.08, minNeighbors=5, minSize=(min_side, min_side))
    candidates = [(rx0 + fx, ry0 + fy, float(fw), float(fh)) for fx, fy, fw, fh in faces]
    candidates = [c for c in candidates if all(_iou(c, t) < 0.3 for t in taken)]
    near = [c for c in candidates if abs(c[0] + c[2] / 2 - cx) < max(bw, bh) and abs(c[1] + c[3] / 2 - cy) < max(bw, bh)]
    if not near:
        return x, y, bw, bh
    best = min(near, key=lambda c: (c[0] + c[2] / 2 - cx) ** 2 + (c[1] + c[3] / 2 - cy) ** 2)
    taken.append(best)
    return best


def detect(photo_path: Path, pid: str, api_key: str) -> dict:
    """One Flash call: subjects with face boxes + photographer/theme review. Saves padded face crops."""
    with Image.open(photo_path) as src:
        im = ImageOps.exif_transpose(src).convert("RGB")
    data = auto._gemini(auto.DIRECTOR_MODEL, [part(im, 1280), {"text": DETECT_BRIEF}], api_key,
                        {"responseMimeType": "application/json", "temperature": 0.2})
    try:
        r = _json(data)
    except (KeyError, IndexError, ValueError) as e:
        raise nw.NanowallError(f"Detection failed: {e}")
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    w, h = im.size
    subjects, taken = [], []
    for i, s in enumerate(r.get("subjects", [])):
        try:
            y0, x0, y1, x1 = [int(v) for v in (s.get("box_2d") or s["face_box"])]
        except (KeyError, ValueError, TypeError):
            continue
        bw, bh = (x1 - x0) * w / 1000, (y1 - y0) * h / 1000
        if s.get("kind", "person") == "person":  # model boxes are approximate: snap onto the real face
            x0, y0, bw, bh = _snap_face(im, x0 * w / 1000, y0 * h / 1000, bw, bh, taken)
            x0, y0 = x0 * 1000 / w, y0 * 1000 / h
            x1, y1 = x0 + bw * 1000 / w, y0 + bh * 1000 / h
        pad = 0.45  # keep hairline, ears and chin: identity lives there too
        box = (max(0, int(x0 * w / 1000 - bw * pad)), max(0, int(y0 * h / 1000 - bh * pad)),
               min(w, int(x1 * w / 1000 + bw * pad)), min(h, int(y1 * h / 1000 + bh * pad)))
        if box[2] - box[0] < 48 or box[3] - box[1] < 48 or bw * bh / (w * h) < MIN_FACE_SHARE:
            continue
        crop = im.crop(box)
        if ImageStat.Stat(crop.convert("L")).stddev[0] < MIN_CROP_STDDEV:
            continue
        crop_file = f"{pid}_{i}.jpg"
        crop.save(FACES_DIR / crop_file, "JPEG", quality=92)
        subjects.append({"kind": s.get("kind", "person"), "label": s.get("label", "subject"),
                         "quality": int(s.get("face_quality", 5)), "crop": crop_file,
                         "face_share": round(bw * bh / (w * h), 4)})
    subjects = verify(subjects, api_key)
    return {"version": DETECT_VERSION, "subjects": subjects, "score": int(r.get("score", 5)), "theme": r.get("theme", ""),
            "why": r.get("why", ""), "critique": r.get("critique", ""), "risks": r.get("risks", "")}


VERIFY_BRIEF = """Each numbered image should be a close crop of ONE face (a person's face, or a dog's/cat's head).
Answer strictly for each. Return ONLY JSON: {"crops": [{"index": n, "is_face": true|false,
"kind": "person|dog|cat", "label": "<neutral 2-4 words, e.g. 'child, light brown hair'; state gender only if obvious>"}]}
is_face is false for foliage, background, clothing, blur, darkness, or a face that is mostly hidden."""


def verify(subjects: list, api_key: str) -> list:
    """Second look at every crop: drop anything that is not clearly a face and correct the labels."""
    if not subjects:
        return subjects
    parts = []
    for i, s in enumerate(subjects):
        with Image.open(FACES_DIR / s["crop"]) as im:
            parts += [{"text": f"#{i}"}, part(im, 384)]
    parts.append({"text": VERIFY_BRIEF})
    checks = None
    for attempt in range(2):  # responses about children are occasionally blocked/empty: try once more
        try:
            checks = {int(c["index"]): c for c in _json(auto._gemini(auto.DIRECTOR_MODEL, parts, api_key,
                      {"responseMimeType": "application/json", "temperature": 0.0}))["crops"]}
            break
        except (nw.NanowallError, KeyError, IndexError, ValueError, TypeError) as e:
            print(f"[identity] verify attempt {attempt + 1} failed: {e}")
    if checks is None:
        return subjects
    kept = []
    for i, s in enumerate(subjects):
        c = checks.get(i)
        if c is None or not c.get("is_face"):
            (FACES_DIR / s["crop"]).unlink(missing_ok=True)
            continue
        kept.append({**s, "kind": c.get("kind", s["kind"]), "label": c.get("label", s["label"])})
    return kept


def cluster(photos: list, api_key: str) -> list:
    """Group every face crop in the album into identities. Returns identities with member crop names."""
    crops = [s["crop"] for p in photos if p.get("detect") for s in p["detect"]["subjects"]]
    crops = [c for c in crops if (FACES_DIR / c).exists()]
    if not crops:
        return []
    parts = []
    for i, c in enumerate(crops):
        with Image.open(FACES_DIR / c) as im:
            parts += [{"text": f"#{i}"}, part(im, 256)]
    parts.append({"text": CLUSTER_BRIEF.format(n=len(crops), last=len(crops) - 1)})
    data = auto._gemini(auto.DIRECTOR_MODEL, parts, api_key, {"responseMimeType": "application/json", "temperature": 0.1})
    try:
        groups = _json(data)["identities"]
    except (KeyError, IndexError, ValueError) as e:
        raise nw.NanowallError(f"Face grouping failed: {e}")
    identities, taken = [], set()
    # identities with fewer members claim first: a crop the model put in two groups goes to the tighter one
    for n, g in enumerate(sorted(groups, key=lambda g: len(g.get("members", [])))):
        members, photos_seen = [], set()
        for i in g.get("members", []):
            if not 0 <= int(i) < len(crops):
                continue
            c, photo_id = crops[int(i)], crops[int(i)].rsplit("_", 1)[0]
            if c in taken or photo_id in photos_seen:  # two faces in one photo are never the same person
                continue
            members.append(c)
            photos_seen.add(photo_id)
            taken.add(c)
        best = [crops[int(i)] for i in g.get("best", []) if 0 <= int(i) < len(crops) and crops[int(i)] in members] or members[:1]
        if members:
            labels = {sub["crop"]: sub["label"] for p in photos if p.get("detect") for sub in p["detect"]["subjects"]}
            identities.append({"id": f"P{n + 1}", "name": labels.get(best[0], g.get("name", "person")),
                               "kind": g.get("kind", "person"), "members": members, "best": best[:3]})
    return identities


def face_pack(identity: dict) -> Image.Image:
    """1-3 best crops side by side on one image: one reference slot per identity."""
    tiles = []
    for c in identity["best"]:
        with Image.open(FACES_DIR / c) as im:
            t = ImageOps.contain(im.convert("RGB"), (512, 512))
            tiles.append(t)
    sheet = Image.new("RGB", (sum(t.width for t in tiles) + 16 * (len(tiles) - 1), max(t.height for t in tiles)), "white")
    x = 0
    for t in tiles:
        sheet.paste(t, (x, 0))
        x += t.width + 16
    return sheet


def pack_for(photo: dict, identities: list) -> list:
    """Identities present in this photo, most prominent first, capped for the consistency budget."""
    crops = {s["crop"]: s for s in photo["detect"]["subjects"]}
    present = [(max(crops[m]["face_share"] for m in i["members"] if m in crops), i)
               for i in identities if any(m in crops for m in i["members"])]
    present.sort(key=lambda x: -x[0])
    people = [i for _, i in present if i["kind"] == "person"][:MAX_IDENTITIES_PER_RENDER]
    pets = [i for _, i in present if i["kind"] != "person"][:1]
    return people + pets


def people_count(photo: dict) -> int:
    return sum(1 for s in photo["detect"]["subjects"] if s["kind"] == "person")
