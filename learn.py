"""Preference learning: test runs of prompt variants, user picks, prompt edits -> a taste profile Claude maintains.

The taste profile (taste.md) is injected into every prompt-writing call, so selections shape future prompts.
"""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import nanowall as nw

RUNS_DIR = nw.ROOT / "runs"
EVENTS_FILE = nw.ROOT / "learnings.jsonl"
TASTE_FILE = nw.ROOT / "taste.md"
MAX_VARIANTS = 4

VARIANTS_BRIEF = """{brief}

MY LEARNED TASTE (follow it; it comes from wallpapers I picked and rejected):
{taste}

Write {n} DISTINCT prompt variants that explore different directions (style, palette, composition)
while respecting my taste. Respond with ONLY a JSON array, no markdown fences:
[{{"style": "2-4 word label", "prompt": "..."}}]"""

LEARN_BRIEF = """You maintain my wallpaper TASTE PROFILE for an image-prompt generator.
Update it using the new evidence. Keep what still holds, sharpen or drop what the evidence contradicts.
Output ONLY the updated profile as 5-12 terse markdown bullets (likes, dislikes, palette, composition,
subject matter). Never mention specific session topics — only transferable aesthetic preferences.

CURRENT PROFILE:
{taste}

NEW EVIDENCE:
{evidence}"""


def get_taste() -> str:
    return TASTE_FILE.read_text(encoding="utf-8").strip() if TASTE_FILE.exists() else ""


def set_taste(text: str) -> None:
    TASTE_FILE.write_text(text.strip() + "\n", encoding="utf-8")


def _log(event: dict) -> None:
    event["at"] = datetime.now().isoformat(timespec="seconds")
    with EVENTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _parse_variants(raw: str, n: int) -> list:
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end < start:
        raise nw.NanowallError(f"Claude didn't return JSON variants: {raw[:300]}")
    try:
        items = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise nw.NanowallError(f"Couldn't parse Claude's variants: {e}")
    variants = [{"style": str(v.get("style", "variant")), "prompt": str(v["prompt"]).strip()}
                for v in items if isinstance(v, dict) and v.get("prompt")]
    if not variants:
        raise nw.NanowallError("Claude returned no usable variants.")
    return variants[:n]


def write_variants(sessions: str, brief: str, n: int) -> list:
    n = max(1, min(MAX_VARIANTS, n))
    base = brief.replace("{sessions}", sessions) if "{sessions}" in brief else f"{brief}\n\nSESSIONS:\n{sessions}"
    text = VARIANTS_BRIEF.format(brief=base.replace("Output ONLY the prompt", "Keep each prompt"),
                                 taste=get_taste() or "(nothing learned yet — explore widely)", n=n)
    return _parse_variants(nw.ask_claude(text), n)


def run_test(sessions: str, brief: str, n: int, api_key: str, model: str, aspect: str, size: str) -> dict:
    variants = write_variants(sessions, brief, n)

    def render(v):
        try:
            path = nw.save_image(nw.generate_image(v["prompt"], api_key, model, aspect, size), v["prompt"])
            return {**v, "file": path.name}
        except nw.NanowallError as e:
            return {**v, "error": str(e)}

    with ThreadPoolExecutor(max_workers=len(variants)) as pool:
        results = list(pool.map(render, variants))
    if all("error" in r for r in results):
        raise nw.NanowallError(results[0]["error"])
    run = {"id": uuid.uuid4().hex[:10], "at": datetime.now().isoformat(timespec="seconds"), "results": results}
    RUNS_DIR.mkdir(exist_ok=True)
    (RUNS_DIR / f"{run['id']}.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
    return run


def load_run(run_id: str) -> dict:
    path = RUNS_DIR / f"{Path(run_id).name}.json"
    if not path.exists():
        raise nw.NanowallError("Unknown test run.")
    return json.loads(path.read_text(encoding="utf-8"))


def _update_taste(evidence: str) -> str:
    taste = nw.ask_claude(LEARN_BRIEF.format(taste=get_taste() or "(empty)", evidence=evidence))
    set_taste(taste)
    return taste


def record_pick(run_id: str, file: str, note: str) -> str:
    run = load_run(run_id)
    ok = [r for r in run["results"] if "file" in r]
    chosen = next((r for r in ok if r["file"] == file), None)
    if not chosen:
        raise nw.NanowallError("That image isn't part of this run.")
    rejected = [r for r in ok if r is not chosen]
    _log({"type": "pick", "run": run_id, "chosen": chosen, "rejected": rejected, "note": note})
    lines = [f"PICKED ({chosen['style']}): {chosen['prompt']}"]
    lines += [f"REJECTED ({r['style']}): {r['prompt']}" for r in rejected]
    if note:
        lines.append(f"MY NOTE ON WHY: {note}")
    return _update_taste("\n".join(lines))


def record_edit(original: str, edited: str, note: str) -> str:
    _log({"type": "edit", "original": original, "edited": edited, "note": note})
    evidence = f"I HAND-EDITED A PROMPT.\nBEFORE: {original}\nAFTER: {edited}"
    if note:
        evidence += f"\nMY NOTE: {note}"
    return _update_taste(evidence)
