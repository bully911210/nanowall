"""nanowall core + CLI: /insights facets -> Claude writes an image prompt -> Nano Banana Pro -> Windows wallpaper.

CLI:  python nanowall.py [--sessions 15] [--dry-run]
App:  python app.py   (prompt refinement, key swapping, history)
"""
import argparse
import base64
import ctypes
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# Frozen .exe: user data lives next to the exe; bundled assets live in the unpack dir.
ROOT = (Path(sys.executable).parent / "nanowall-data") if getattr(sys, "frozen", False) else Path(__file__).parent
ROOT.mkdir(exist_ok=True)  # portable: everything the app writes stays in this one folder beside the exe
ASSETS = Path(getattr(sys, "_MEIPASS", ROOT))
FACETS_DIR = Path.home() / ".claude" / "usage-data" / "facets"
OUT_DIR = ROOT / "wallpapers"
DEFAULT_MODEL = os.environ.get("NANOWALL_MODEL", "gemini-3-pro-image-preview")  # Nano Banana Pro

DEFAULT_BRIEF = """You write ONE image-generation prompt for a 16:9 desktop wallpaper.
Below are summaries of my recent Claude Code sessions (from /insights).
Distil the themes, mood and what I've been building into a striking, abstract-leaning
cinematic scene. No text, no logos, no UI screenshots, no people's faces.
Keep the centre calm (desktop icons sit on the left). Output ONLY the prompt, max 120 words.

SESSIONS:
{sessions}"""


class NanowallError(RuntimeError):
    """User-facing failure in any pipeline step."""


class SafetyBlocked(NanowallError):
    """Gemini filtered the output. Not an outage: skip this item and move on."""


def load_insights(limit: int) -> str:
    if not FACETS_DIR.exists():
        raise NanowallError(f"No insights data at {FACETS_DIR}. Run /insights in Claude Code first.")
    files = sorted(FACETS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    lines = []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        lines.append(f"- {d.get('underlying_goal', '')} | {d.get('brief_summary', '')}")
    if not lines:
        raise NanowallError("Insights facets were empty.")
    return "\n".join(lines)


def write_prompt(sessions: str, brief: str = DEFAULT_BRIEF) -> str:
    text = brief.replace("{sessions}", sessions) if "{sessions}" in brief else f"{brief}\n\nSESSIONS:\n{sessions}"
    taste_file = ROOT / "taste.md"
    if taste_file.exists():
        text += f"\n\nMY LEARNED TASTE (follow it):\n{taste_file.read_text(encoding='utf-8')}"
    return ask_claude(text)


def ask_claude(text: str) -> str:
    try:
        result = subprocess.run(
            ["claude", "-p"], input=text,  # stdin: cmd.exe mangles newlines in args
            capture_output=True, text=True, encoding="utf-8", shell=True, timeout=240,
        )
    except subprocess.TimeoutExpired:
        raise NanowallError("claude -p timed out after 240s.")
    out = result.stdout.strip()
    # non-zero exit alone isn't fatal: SessionEnd hooks can fail after a good answer
    if not out or out.startswith(("Failed to authenticate", "Error:", "Invalid API key")):
        raise NanowallError(f"claude -p failed: {out or result.stderr.strip()[:400]}"
                            " (if auth expired: run `claude` in a terminal to log in again)")
    return out


def generate_image(prompt: str, api_key: str, model: str = DEFAULT_MODEL,
                   aspect: str = "16:9", size: str = "4K") -> bytes:
    if not api_key:
        raise NanowallError("No Gemini API key selected.")
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": aspect, "imageSize": size},
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise NanowallError(f"Gemini error {e.code}: {e.read().decode(errors='replace')[:600]}")
    except urllib.error.URLError as e:
        raise NanowallError(f"Network error reaching Gemini: {e.reason}")
    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            if "inlineData" in part:
                return base64.b64decode(part["inlineData"]["data"])
    raise NanowallError(f"No image returned: {json.dumps(data)[:500]}")


def save_image(img: bytes, prompt: str) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")  # microseconds: parallel renders must not overwrite each other
    path = OUT_DIR / f"wall-{stamp}.png"
    path.write_bytes(img)
    path.with_suffix(".txt").write_text(prompt, encoding="utf-8")
    return path


def set_wallpaper(path: Path) -> None:
    SPI_SETDESKWALLPAPER, SPIF_UPDATEINIFILE, SPIF_SENDCHANGE = 20, 0x01, 0x02
    ok = ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER, 0, str(path.resolve()), SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
    )
    if not ok:
        raise NanowallError("Windows refused the wallpaper change.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=15)
    ap.add_argument("--dry-run", action="store_true", help="print the prompt, skip Gemini")
    args = ap.parse_args()
    try:
        print("1/3 reading insights + asking Claude for a prompt...")
        prompt = write_prompt(load_insights(args.sessions))
        print(f"\nPROMPT:\n{prompt}\n")
        if args.dry_run:
            return
        print("2/3 generating with Nano Banana Pro...")
        path = save_image(generate_image(prompt, os.environ.get("GEMINI_API_KEY", "")), prompt)
        print("3/3 setting wallpaper...")
        set_wallpaper(path)
        print(f"done -> {path}")
    except NanowallError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
