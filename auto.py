"""Autopilot: approved personal photos + insights + learned taste -> Gemini art-directs -> Nano Banana Pro renders.

One key, zero typing. Each variant is built on a DIFFERENT personal photo and a different creative
direction, so runs never collapse into the same image.
"""
import base64
import io
import json
import random
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from PIL import Image, ImageStat

import learn
import nanowall as nw
import photos

DIRECTOR_MODEL = "gemini-2.5-flash"
DIRECTIONS = [  # every direction forces a change of MEDIUM; "photo + filter" styles are banned
    "retro 70s screen-printed travel poster", "hand-painted Pixar-style 3D animation still",
    "Studio Ghibli watercolour background art", "layered papercut diorama lit from behind",
    "claymation miniature set with tilt-shift depth", "ukiyo-e woodblock print",
    "thick impasto oil painting with visible knife strokes", "luminous stained-glass mural",
    "vintage Soviet-era children's book illustration", "tiny tilt-shift miniature world at dusk",
    "art deco railway poster in gold and teal", "dreamlike surreal scale shift painting"]

DIRECTOR_BRIEF = """You are an art director making 16:9 desktop wallpapers for ONE person from THEIR OWN photos.
The photos are numbered 0..{last}. Study what they photograph and keep: places, animals, objects,
light, colour, mood. That reveals their taste far better than words.

What they work on lately (context, use lightly):
{sessions}

Learned taste so far:
{taste}

Return ONLY JSON, no fences:
{{"taste": "6-10 terse markdown bullets of transferable aesthetic preferences inferred from the photos + taste",
  "concepts": [{{"ref": <photo index>, "style": "<2-4 word label>",
                "prompt": "<80-120 word image prompt that transforms photo <ref> into a striking wallpaper in the direction '<direction>'>"}}]}}

Rules: exactly {n} concepts, each using a DIFFERENT photo index and its assigned direction in order: {directions}.
- TRANSFORM, don't filter: invent a brand-new widescreen scene that fully commits to the direction
  (medium, brushwork, lighting, world). A lightly retouched photo is a failure.
- The people/place/animal from the photo stay RECOGNISABLE: same faces, hair, clothing cues, likeness.
- Re-stage for 16:9: the scene extends naturally edge to edge. Main subjects sit right of centre;
  the left side holds quiet but real scenery (sky, field, water, soft bokeh). NEVER bars, panels,
  borders, frames, split layouts or empty colour blocks.
- Describe the full environment, camera, light and medium concretely.
- No text, letters, numbers, logos or watermarks anywhere (remove any from the source)."""

CRITIC_BRIEF = """Judge this desktop wallpaper HARSHLY. The reference photo is attached first, the wallpaper second.
Intended direction: {style}.
Score 1-10 each: transformation (a genuinely new artwork in that direction, not a filtered photo),
likeness (people/place still recognisable), frame (fills 16:9 edge to edge, NO bars/panels/empty blocks,
subjects not dead centre), clean (no text/letters/logos), wow (would someone keep it as their wallpaper).
Return ONLY JSON: {{"transformation":n,"likeness":n,"frame":n,"clean":n,"wow":n,
"fix":"one concrete sentence telling the image model what to change"}}"""
PASS_SCORE = 7


def _gemini(model: str, parts: list, api_key: str, config: dict, timeout: int = 300) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    req = urllib.request.Request(url, method="POST",
                                 data=json.dumps({"contents": [{"parts": parts}], "generationConfig": config}).encode(),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise nw.NanowallError(f"Gemini {model} error {e.code}: {e.read().decode(errors='replace')[:500]}")
    except urllib.error.URLError as e:
        raise nw.NanowallError(f"Network error reaching Gemini: {e.reason}")


def _img_part(path: str, side: int) -> dict:
    return {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(photos.jpeg_bytes(path, side)).decode()}}


def direct(refs: list, n: int, api_key: str) -> dict:
    directions = random.sample(DIRECTIONS, n)
    try:
        sessions = nw.load_insights(10)
    except nw.NanowallError:
        sessions = "(none)"
    text = DIRECTOR_BRIEF.format(last=len(refs) - 1, sessions=sessions, n=n, directions=directions,
                                 taste=learn.get_taste() or "(nothing yet)")
    parts = [_img_part(p, 512) for p in refs] + [{"text": text}]
    data = _gemini(DIRECTOR_MODEL, parts, api_key, {"responseMimeType": "application/json", "temperature": 1.1})
    try:
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        plan = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        concepts = [c for c in plan["concepts"] if 0 <= int(c["ref"]) < len(refs) and c.get("prompt")][:n]
    except (KeyError, IndexError, ValueError, TypeError) as e:
        raise nw.NanowallError(f"Art director returned an unusable plan: {e}")
    if not concepts:
        raise nw.NanowallError("Art director returned no concepts.")
    return {"taste": plan.get("taste", ""), "concepts": concepts}


RENDER_PREFIX = ("The attached photo is a LIKENESS REFERENCE ONLY for the people, place or subject. "
                 "Do NOT reproduce its framing, crop, aspect or photographic look. Create a completely new "
                 "16:9 widescreen artwork that fills the whole frame edge to edge, with no bars, borders, "
                 "panels or text. Change the MEDIUM completely (it must not look like a photo). Remove all "
                 "writing, slogans and logos from clothing and objects. Keep faces recognisable.\n\n")


def _jpeg_from_bytes(img: bytes, side: int = 768) -> str:
    with Image.open(io.BytesIO(img)) as im:
        im = im.convert("RGB")
        im.thumbnail((side, side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def has_bars(img: bytes) -> bool:
    """Local check: a flat, featureless strip on either side means the model letterboxed the image."""
    with Image.open(io.BytesIO(img)) as im:
        g = im.convert("L")
        w, h = g.size
        return any(ImageStat.Stat(g.crop(box)).stddev[0] < 6
                   for box in ((0, 0, int(w * 0.12), h), (int(w * 0.88), 0, w, h)))


def critique(ref_path: str, img: bytes, style: str, api_key: str) -> dict:
    parts = [_img_part(ref_path, 512),
             {"inlineData": {"mimeType": "image/jpeg", "data": _jpeg_from_bytes(img)}},
             {"text": CRITIC_BRIEF.format(style=style)}]
    try:
        data = _gemini(DIRECTOR_MODEL, parts, api_key, {"responseMimeType": "application/json", "temperature": 0.2})
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        v = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        scores = {k: int(v.get(k, 5)) for k in ("transformation", "likeness", "frame", "clean", "wow")}
    except (nw.NanowallError, KeyError, IndexError, ValueError, TypeError) as e:
        print(f"[auto] critic unavailable, not blocking: {e}")
        return {"score": PASS_SCORE, "fix": ""}
    score = min(scores.values())
    if scores["clean"] < 9:  # any visible text/logo is a dealbreaker, not a minor deduction
        score = min(score, 4)
    return {**scores, "score": score, "fix": str(v.get("fix", ""))}


def render(prompt: str, ref_path: str, api_key: str, model: str, size: str) -> bytes:
    parts = [_img_part(ref_path, 1024), {"text": RENDER_PREFIX + prompt}]
    data = _gemini(model, parts, api_key, {"responseModalities": ["IMAGE"],
                                           "imageConfig": {"aspectRatio": "16:9", "imageSize": size}})
    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            if "inlineData" in part:
                return base64.b64decode(part["inlineData"]["data"])
    raise nw.NanowallError(f"No image returned: {json.dumps(data)[:300]}")


def autopilot(api_key: str, n: int = 3, model: str = nw.DEFAULT_MODEL, size: str = "2K", apply_first: bool = True) -> dict:
    if not api_key:
        raise nw.NanowallError("No Gemini API key.")
    lib = photos.load_library()
    refs = photos.pick_refs(lib, max(n, 4))
    if not refs:
        raise nw.NanowallError("No approved photos yet. Scan and approve some first.")
    n = max(1, min(4, n, len(refs)))
    plan = direct(refs, n, api_key)
    taste = plan["taste"]
    if isinstance(taste, list):  # Gemini sometimes returns bullets as a JSON array
        taste = chr(10).join(t if str(t).lstrip().startswith("-") else f"- {t}" for t in taste)
    if isinstance(taste, str) and taste.strip():
        learn.set_taste(taste)

    def one(c):
        ref = refs[int(c["ref"])]
        style = c.get("style", "variant")
        try:
            prompt, best = c["prompt"], None
            for attempt in range(3):  # render, judge, and redo once with the critic's fix if it's weak
                img = render(prompt, ref, api_key, model, size)
                verdict = critique(ref, img, style, api_key)
                if has_bars(img):
                    verdict = {**verdict, "frame": 1, "score": 1,
                               "fix": "Fill the entire frame edge to edge; remove the flat side bar. " + verdict["fix"]}
                if best is None or verdict["score"] > best[1]["score"]:
                    best = (img, verdict, prompt)
                if verdict["score"] >= PASS_SCORE:
                    break
                prompt = f"{c['prompt']}\n\nCRITICAL FIX FROM REVIEW: {verdict['fix']}"
            img, verdict, prompt = best
            path = nw.save_image(img, prompt)
            return {"style": style, "prompt": prompt, "ref": ref, "file": path.name,
                    "score": verdict["score"], "verdict": verdict, "attempts": attempt + 1}
        except nw.NanowallError as e:
            return {"style": c.get("style", "variant"), "prompt": c["prompt"], "ref": ref, "error": str(e)}

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(one, plan["concepts"]))
    ok = sorted([r for r in results if "file" in r], key=lambda r: -r.get("score", 0))  # best goes on the desktop
    if any(r.get("score", 0) >= PASS_SCORE for r in ok):  # hide failures when something good exists
        results = [r for r in results if "file" not in r or r.get("score", 0) >= PASS_SCORE]
        ok = [r for r in ok if r.get("score", 0) >= PASS_SCORE]
    if not ok:
        raise nw.NanowallError(results[0]["error"])
    for r in ok:
        lib["used"][r["ref"]] = lib["used"].get(r["ref"], 0) + 1
    photos.save_library(lib)
    if apply_first:
        nw.set_wallpaper(nw.OUT_DIR / ok[0]["file"])
    run = {"id": uuid.uuid4().hex[:10], "at": datetime.now().isoformat(timespec="seconds"),
           "results": results, "taste": learn.get_taste(), "applied": ok[0]["file"] if apply_first else None}
    learn.RUNS_DIR.mkdir(exist_ok=True)
    (learn.RUNS_DIR / f"{run['id']}.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
    return run


if __name__ == "__main__":  # scheduled, zero-input run: python auto.py
    import os, sys
    keys = json.loads((nw.ROOT / "keys.json").read_text(encoding="utf-8")) if (nw.ROOT / "keys.json").exists() else {}
    key = keys.get("keys", {}).get(keys.get("active"), "") or os.environ.get("GEMINI_API_KEY", "")
    try:
        r = autopilot(key, n=1, size="4K")
        print(f"wallpaper set -> {r['applied']}")
    except nw.NanowallError as e:
        sys.exit(str(e))
