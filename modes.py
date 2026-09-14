"""Stages B-D: LOCK first (prove it is them), then STYLE from the lock. One change per render.
Carousel quota is by recognisability, not by medium.
"""
import base64
import io
import json
import random

from PIL import Image

import auto
import identity as idn
import nanowall as nw

# Stage D: out of 50 slots
QUOTA = {"LOCK": 20, "LIGHT": 15, "FACED": 10, "WILDCARD": 5}
LOCK_CHANGES = [
    "crop and extend to a 16:9 widescreen frame, with the people in the right half and quiet scenery on the left",
    "replace the background with a quiet, uncluttered version of the same kind of place",
    "soften the light to late-afternoon golden light",
    "apply a gentle cinematic colour grade and clean up background clutter",
]
STYLES = {
    "LIGHT": ["a realistic oil painting", "a detailed digital painting", "a 1970s Kodachrome photograph"],
    "FACED": ["a watercolor painting", "a graphic novel panel with clean inks"],
    "WILDCARD": ["a flat vector colour-field artwork", "a pixel-art scene", "an abstract geometric composition"],
}
SAFETY_REASONS = {"IMAGE_SAFETY", "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "IMAGE_PROHIBITED_CONTENT", "OTHER"}
SAFETY_SKIP_LIMIT = 3  # a photo filtered this many times is dropped from the rotation

# Minimum critic scores per mode. WILDCARD makes no face promise.
LIKENESS_MIN = {"LOCK": 8, "LIGHT": 7, "FACED": 6, "WILDCARD": 0}


def next_mode(carousel: list) -> str:
    made = {m: sum(1 for c in carousel if c.get("mode") == m) for m in QUOTA}
    total = sum(QUOTA.values())
    filled = len(carousel) + 1
    # biggest shortfall against the target mix so far; LOCK wins ties because it is the product
    return max(QUOTA, key=lambda m: (QUOTA[m] * filled / total - made[m], m == "LOCK"))


def _labels(pack: list, first: str) -> list:
    # neutral role names only: a wrong descriptive label ("boy") pulls the model toward a different person
    lines, people = [first], 0
    for n, ident in enumerate(pack, start=2):
        if ident["kind"] == "person":
            people += 1
            lines.append(f"Image {n}: identity crops of person {people} in Image 1.")
        else:
            lines.append(f"Image {n}: identity crops of the {ident['kind']} in Image 1.")
    return lines


def lock_request(source: Image.Image, pack: list, change: str) -> list:
    lines = _labels(pack, "Image 1: source photo.")
    has_pet = any(i["kind"] != "person" for i in pack)
    text = "\n".join(lines) + f"""

Use the people{' and animal' if has_pet else ''} from these images. Image 1 is the identity source: keep these exact faces.
Keep facial structure, age, skin, hairline{', and the animal markings' if has_pet else ''} exactly.
Do not replace them with similar-looking subjects. Do not invent a similar child.

Only change: {change}.
Keep the original pose, expressions and clothes (same garments and colours), but remove any printed words,
slogans or logos from the clothing.
Photorealistic. No new people. No text, letters or logos. 16:9."""
    return _parts(source, pack, text)


def style_request(lock_img: Image.Image, pack: list, style: str, mode: str) -> list:
    lines = _labels(pack, "Image 1 is the approved portrait of these exact people.")
    if mode == "WILDCARD":
        text = "\n".join(lines) + f"""

Reinterpret Image 1 as {style} wallpaper: the colours, mood and composition of this day.
Figures may be abstracted. No text, letters or logos. 16:9, fill the frame edge to edge."""
    else:
        text = "\n".join(lines) + f"""

Restyle the WHOLE of Image 1 as {style} wallpaper: people, clothing, background and light all rendered
consistently in that medium, with its real texture. No flat pasted patches or cut-out areas.
Keep the same faces and the same spatial relationship. Do not redraw them as different people.
Clothing keeps its colours. Faces stay recognisable.
No text, letters or logos anywhere, including on clothing. 16:9, fill the frame edge to edge."""
    return _parts(lock_img, pack, text)


def _parts(first: Image.Image, pack: list, text: str) -> list:
    parts = [idn.part(first, 1536)]
    for ident in pack:
        parts.append(idn.part(idn.face_pack(ident), 1024))
    parts.append({"text": text})
    return parts


def render(parts: list, api_key: str, size: str = "4K") -> bytes:
    data = auto._gemini(nw.DEFAULT_MODEL, parts, api_key,
                        {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "16:9", "imageSize": size}})
    for cand in data.get("candidates", []):
        for p in cand.get("content", {}).get("parts", []):
            if "inlineData" in p:
                return base64.b64decode(p["inlineData"]["data"])
    reasons = {c.get("finishReason", "") for c in data.get("candidates", [])}
    reasons.add(data.get("promptFeedback", {}).get("blockReason", ""))
    if reasons & SAFETY_REASONS:
        raise nw.SafetyBlocked(f"filtered by Gemini ({', '.join(sorted(r for r in reasons if r))})")
    raise nw.NanowallError(f"No image returned: {json.dumps(data)[:300]}")


CRITIC = """First image: the generated wallpaper. Following images: identity crops of the real people/animals.
Mode: {mode}. {promise}
Score 1-10: recognisable (would a grandparent recognise these exact people at a 5-second glance? 10 = unmistakably them;
score 10 if there are no people/animals), frame (fills 16:9 edge to edge, no bars or empty panels),
clean (no text, letters, logos or watermarks anywhere, INCLUDING printed on clothing; 1 if any word is readable),
quality (no extra limbs, melted hands, duplicate or new people),
style (the WHOLE image is genuinely rendered as: {style}. No flat colour patches, cut-outs or areas left untouched;
for LOCK score photographic realism).
Return ONLY JSON: {{"recognisable": n, "frame": n, "clean": n, "quality": n, "style": n, "fix": "<one sentence>"}}"""


def judge(img: bytes, pack: list, mode: str, api_key: str, style: str = "photorealistic photograph") -> dict:
    promise = "No face promise: judge recognisable leniently." if mode == "WILDCARD" else "Faces must be the real people."
    with Image.open(io.BytesIO(img)) as im:
        parts = [idn.part(im, 1024)]
    parts += [idn.part(idn.face_pack(i), 768) for i in pack]
    parts.append({"text": CRITIC.format(mode=mode, promise=promise, style=style)})
    try:
        v = idn._json(auto._gemini(auto.DIRECTOR_MODEL, parts, api_key, {"responseMimeType": "application/json", "temperature": 0.1}))
        s = {k: int(v.get(k, 5)) for k in ("recognisable", "frame", "clean", "quality", "style")}
    except (nw.NanowallError, KeyError, IndexError, ValueError, TypeError) as e:
        print(f"[modes] judge unavailable: {e}")
        return {"passed": False, "fix": "", "recognisable": 0}
    passed = (s["recognisable"] >= LIKENESS_MIN[mode] and s["frame"] >= 7 and s["clean"] >= 9
              and s["quality"] >= 7 and s["style"] >= 7 and not auto.has_bars(img))
    return {**s, "passed": passed, "fix": str(v.get("fix", ""))}


def pick_change() -> str:
    return random.choice(LOCK_CHANGES)


def pick_style(mode: str, used: list) -> str:
    fresh = [s for s in STYLES[mode] if s not in used]
    return random.choice(fresh or STYLES[mode])
