"""Render every product across the four production stacks and record what it cost.

THE GRID
--------
Four arms, twenty renders a product. Three arms are HERMES' prompt on the three
models its chain can reach; one is VNYX-API's prompt on the model it actually
runs. That is the comparison the question asks for — two production stacks, not
an academic 2x4 — so the prompt is pinned to the side that ships it:

    vnyx-gemini-pro      vnyx prompt    gemini-3-pro-image-preview
    hermes-gemini-flash  hermes prompt  gemini-3.1-flash-image
    hermes-gpt-medium    hermes prompt  gpt-image-1.5  quality=medium
    hermes-gpt-low       hermes prompt  gpt-image-1.5  quality=low

FIVE VIEWS, AND THE CATEGORY DECIDES WHAT THEY MEAN
---------------------------------------------------
front, back, front34, back34, closeup. The NAMES are fixed; the framing behind
them is not. A ¾ view is knee-up on a shirt, waist-to-feet on jeans,
knee-to-floor on a shoe and cropped to the region on an accessory — that
per-category resolution is the whole subject of the comparison, and it happens
inside the two prompt builders, not here.

ONE HUMAN MODEL PER PRODUCT
---------------------------
The personality is read from the LOCAL database's model library and pinned per
gender for the whole run, so all twenty renders of a product — and every product
of that gender — show the same person. Without it each render draws its own face
and nothing can be compared: a difference between two pictures would be a
different model, not a different prompt.

ASPECT RATIO IS LEFT UNCONSTRAINED, DELIBERATELY
------------------------------------------------
Both pipelines send no `aspect_ratio` when the tenant's setting is "auto", which
is the common case, so this does the same. It is not tidy — the jeans run
measured Pro at 1792x2386 and Flash at 2048x2048 SQUARE on 5 of 6 renders — but
pinning a ratio here would hide a real production defect behind a benchmark
convenience. The shape of every render is recorded and belongs in the report.

READS THE LOCAL DATABASE ONLY, for the model library, and refuses any DSN that is
not localhost. Writes nothing to it.

    python scripts/bench_images.py --all
    python scripts/bench_images.py --product "purse for women" --filing gap
    python scripts/bench_images.py --all --dry-run
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.imaging.nanobanana import (  # noqa: E402
    PromptContext, apply_personality, build_prompt, classify_garment_type,
    compress_source,
)
from app.models import ImagerySettings  # noqa: E402

ROOT = Path(r"C:\Users\Mahesh\Desktop\products test items")
OUT_ROOT = ROOT / "_bench_out"
VNYX_DIST = Path(r"E:\vnyx\vnyx-api\dist\services\shot-prompt.js")

# view -> the vnyx-api ShotKey that means the same thing. The two builders name
# the same five framings differently; this is the only place that mapping lives.
VIEW_TO_SHOT = {
    "front": "full_front",
    "back": "full_back",
    "front34": "three_quarter_front",
    "back34": "three_quarter_back",
    "closeup": "closeup",
}
# Anchor FIRST — every later view is conditioned on it for model identity.
DEFAULT_VIEWS = ["front", "back", "front34", "back34", "closeup"]

VIEW_LABELS = {
    "front": "Full front",
    "back": "Full back",
    "front34": "Three-quarter front",
    "back34": "Three-quarter back",
    "closeup": "Close-up",
}


@dataclass(frozen=True)
class Arm:
    """One production stack: a prompt builder bolted to a model."""

    key: str
    prompt: str            # "vnyx" | "hermes"
    vendor: str            # "gemini" | "openai"
    model: str
    quality: str | None = None
    note: str = ""


ARMS: list[Arm] = [
    Arm("vnyx-gemini-pro", "vnyx", "gemini", "gemini-3-pro-image-preview",
        note="vnyx-api as it ships today (banana-nano.ts:23)"),
    Arm("hermes-gemini-flash", "hermes", "gemini", "gemini-3.1-flash-image",
        note="Hermes primary (config/policy.yaml)"),
    Arm("hermes-gpt-medium", "hermes", "openai",
        os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1.5"), quality="medium",
        note="Hermes fallback at the configured tier"),
    Arm("hermes-gpt-low", "hermes", "openai",
        os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1.5"), quality="low",
        note="Hermes fallback at the cheap tier"),
]

# $ per 1M tokens, confirmed against the vendors' own pricing pages (22 Sep 2026:
# ai.google.dev/gemini-api/docs/pricing,
# developers.openai.com/api/docs/models/gpt-image-1.5).
#
# `image_in` is separate because OpenAI bills image input at a different rate
# from text input and reports the split. On this workload that is not a rounding
# detail — every call carries two or three reference photographs.
PRICES: dict[str, dict[str, float]] = {
    "gemini-3-pro-image-preview": {"in": 2.00, "image_in": 2.00, "out": 120.0},
    "gemini-3.1-flash-image": {"in": 0.50, "image_in": 0.50, "out": 60.0},
    "gemini-3.1-flash-image-preview": {"in": 0.50, "image_in": 0.50, "out": 60.0},
    "gpt-image-1.5": {"in": 5.00, "image_in": 8.00, "out": 32.0},
    "gpt-image-1": {"in": 5.00, "image_in": 10.0, "out": 40.0},
}


def env() -> dict[str, str]:
    """Read .env directly, so this never imports a settings module."""
    path = Path(__file__).resolve().parents[1] / ".env"
    text = path.read_text(encoding="utf-8", errors="replace")
    return {k: v.strip() for k, v in re.findall(r"^([A-Z_0-9]+)=(.*)$", text, re.M)}


# --------------------------------------------------------------------------- #
# The model library
# --------------------------------------------------------------------------- #

def load_personalities(dsn: str) -> dict[str, dict[str, Any]]:
    """One adult personality per gender, from the tenant that has them enabled.

    Deterministic — first by name — so a re-run picks the same two people and a
    second batch of renders can sit beside the first. A random draw would make
    every re-run incomparable with the last, which is the one thing a benchmark
    cannot afford.
    """
    import psycopg

    # The DSN is read from .env, but a benchmark must never be the thing that
    # reaches a shared database, so the guard is here rather than assumed.
    if "localhost" not in dsn and "127.0.0.1" not in dsn:
        raise SystemExit(f"refusing a non-local database: {dsn.split('@')[-1]}")

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT "tenantId", "personalities" FROM "ImageGenerationSettings" '
            'WHERE "personalitiesEnabled" = true'
        )
        rows = cur.fetchall()

    chosen: dict[str, dict[str, Any]] = {}
    for tenant_id, people in rows:
        adults = [
            p for p in (people or [])
            if isinstance(p, dict) and p.get("enabled") is not False
            and p.get("group") != "kids"
        ]
        for gender in ("female", "male"):
            if gender in chosen:
                continue
            pool = sorted(
                (p for p in adults if p.get("gender") == gender),
                key=lambda p: str(p.get("name") or ""),
            )
            if pool:
                # str(), because psycopg hands back a UUID object and this dict
                # is written into context.json for the report.
                chosen[gender] = {**pool[0], "_tenantId": str(tenant_id)}
        if len(chosen) == 2:
            break

    if not chosen:
        raise SystemExit("no enabled adult personalities in the local model library")
    return chosen


def describe(person: dict[str, Any]) -> str:
    bits = [
        f"{person.get('name')}",
        f"{person.get('age')}y",
        person.get("gender") or "",
        person.get("skinTone") or "",
        " ".join(x for x in (person.get("hairColor"), person.get("hairStyle")) if x),
    ]
    return " · ".join(b for b in bits if b)


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

def hermes_prompts(meta: dict[str, Any], category: str, sub: str,
                   views: list[str], person: dict[str, Any]) -> dict[str, str]:
    settings = ImagerySettings(
        background=meta.get("background"),
        gender=meta["gender"],
        age=meta.get("age"),
        body_type=meta.get("bodyType"),
    )
    # The same flattening the live generator does, so the prompt carries the
    # library's traits rather than a generic model.
    settings = apply_personality(settings, person)
    ctx = PromptContext(
        settings=settings,
        category=category,
        sub_category=sub,
        mannequin_type=meta.get("mannequinType"),
        gender=meta["gender"],
        back_inferred=not meta["images"].get("back"),
    )
    return {
        v: build_prompt(v, ctx, has_front_reference=(v != "front"))
        for v in views
    }


def vnyx_prompts(meta: dict[str, Any], category: str, sub: str,
                 views: list[str], person: dict[str, Any]) -> dict[str, str]:
    """Call the COMPILED vnyx-api builder, so this cannot drift from what ships."""
    import subprocess

    spec = {
        "dist": VNYX_DIST.as_uri(),
        "settings": {
            "background": meta.get("background"),
            "gender": meta["gender"],
            # The personality's own age wins over the tenant default, matching
            # apply_personality on the Hermes side.
            "age": person.get("age") or meta.get("age"),
            "bodyType": meta.get("bodyType"),
            "mannequinType": meta.get("mannequinType"),
            "category": category,
            "subCategory": sub,
            "skinTone": person.get("skinTone"),
            "hairColor": person.get("hairColor"),
            "hairStyle": person.get("hairStyle"),
            "tattoos": person.get("tattoos"),
            "piercings": person.get("piercings"),
            "personalityNotes": person.get("notes"),
        },
        "shots": [
            {"view": v, "shot": VIEW_TO_SHOT[v], "hasFrontReference": v != "front"}
            for v in views
        ],
    }
    script = """
const spec = JSON.parse(process.argv[1]);
const m = await import(spec.dist);
const out = {};
for (const s of spec.shots) {
  out[s.view] = m.buildShotPrompt('nano-banana', {
    shot: s.shot, variation: 1, variationCount: 1,
    hasFrontReference: s.hasFrontReference, settings: spec.settings,
  });
}
process.stdout.write(JSON.stringify(out));
"""
    res = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(spec)],
        capture_output=True, text=True, timeout=60,
    )
    if res.returncode != 0:
        raise RuntimeError(f"vnyx prompt build failed: {res.stderr[:400]}")
    return json.loads(res.stdout)


BUILDERS = {"vnyx": vnyx_prompts, "hermes": hermes_prompts}


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #

@dataclass
class Render:
    ok: bool
    data: bytes | None = None
    error: str | None = None
    seconds: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)


def render_gemini(arm: Arm, prompt: str, images: list[bytes], api_key: str,
                  resolution: str) -> Render:
    from google import genai
    from google.genai import types

    parts: list[Any] = [types.Part.from_text(text=prompt)]
    for data in images:
        parts.append(types.Part.from_bytes(data=data, mime_type="image/jpeg"))

    client = genai.Client(
        api_key=api_key, http_options=types.HttpOptions(timeout=300_000)
    )
    config = types.GenerateContentConfig(
        response_modalities=["IMAGE", "TEXT"],
        image_config=types.ImageConfig(image_size=resolution),
    )
    started = time.perf_counter()
    try:
        resp = client.models.generate_content(
            model=arm.model,
            contents=[types.Content(role="user", parts=parts)],
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 — one view failing is survivable
        return Render(False, error=f"{type(exc).__name__}: {exc}",
                      seconds=time.perf_counter() - started)
    seconds = time.perf_counter() - started

    um = getattr(resp, "usage_metadata", None)
    usage = {
        "input_tokens": getattr(um, "prompt_token_count", None),
        "output_tokens": getattr(um, "candidates_token_count", None),
        "total_tokens": getattr(um, "total_token_count", None),
    }

    for candidate in resp.candidates or []:
        for part in (candidate.content.parts if candidate.content else []) or []:
            inline = getattr(part, "inline_data", None)
            if inline and inline.data:
                return Render(True, data=inline.data, seconds=seconds, usage=usage)

    # No image and no exception is a content decision, not a fault — and the
    # reason lives in three places, so checking only the first reports a refused
    # garment as "no reason given".
    reasons: list[str] = []
    block = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
    if block:
        reasons.append(f"prompt blocked: {block}")
    for candidate in resp.candidates or []:
        finish = getattr(candidate, "finish_reason", None)
        if finish:
            reasons.append(str(finish))
        for part in (candidate.content.parts if candidate.content else []) or []:
            said = (getattr(part, "text", "") or "").strip()
            if said:
                reasons.append(f'model replied: "{said[:160]}"')
        break
    return Render(False, error=" — ".join(reasons) or "no image and no reason given",
                  seconds=seconds, usage=usage)


def render_openai(arm: Arm, prompt: str, images: list[bytes], api_key: str) -> Render:
    import httpx

    files = (
        [("image", ("source.jpg", images[0], "image/jpeg"))]
        if len(images) == 1
        else [
            (f"image[{i}]", (f"source-{i}.jpg", data, "image/jpeg"))
            for i, data in enumerate(images)
        ]
    )
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=300.0) as client:
            resp = client.post(
                "https://api.openai.com/v1/images/edits",
                headers={"Authorization": f"Bearer {api_key}"},
                files=files,
                data={
                    "model": arm.model, "prompt": prompt, "size": "1024x1536",
                    "n": "1", "quality": arm.quality or "medium",
                },
            )
    except Exception as exc:  # noqa: BLE001
        return Render(False, error=f"{type(exc).__name__}: {exc}",
                      seconds=time.perf_counter() - started)
    seconds = time.perf_counter() - started

    if resp.status_code != 200:
        detail = resp.text[:300]
        try:
            detail = resp.json().get("error", {}).get("message", detail)
        except Exception:  # noqa: BLE001
            pass
        return Render(False, error=f"openai {resp.status_code}: {detail}",
                      seconds=seconds)

    payload = resp.json()
    raw = payload.get("usage") or {}
    usage = {
        "input_tokens": raw.get("input_tokens"),
        "output_tokens": raw.get("output_tokens"),
        "total_tokens": raw.get("total_tokens"),
        "input_tokens_details": raw.get("input_tokens_details"),
    }
    b64 = (payload.get("data") or [{}])[0].get("b64_json")
    if not b64:
        return Render(False, error="openai returned no image", seconds=seconds,
                      usage=usage)
    return Render(True, data=base64.b64decode(b64), seconds=seconds, usage=usage)


def cost_of(model: str, usage: dict[str, Any]) -> float | None:
    """Dollars for one call, from the tokens the provider reported."""
    price = PRICES.get(model)
    if not price or usage.get("output_tokens") is None:
        return None
    total_in = usage.get("input_tokens") or 0
    details = usage.get("input_tokens_details") or {}
    image_in = details.get("image_tokens")
    if image_in is None:
        text_in, image_in = total_in, 0
    else:
        text_in = max(0, total_in - image_in)
    out = usage.get("output_tokens") or 0
    return round(
        (text_in * price["in"] + image_in * price["image_in"] + out * price["out"])
        / 1e6, 5,
    )


def shape_of(data: bytes) -> str:
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
        from fractions import Fraction
        fr = Fraction(w, h).limit_denominator(60)
        return f"{w}x{h} ({fr.numerator}:{fr.denominator})"
    except Exception:  # noqa: BLE001
        return "?"


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #

def run_cell(arm: Arm, prompts: dict[str, str], front: bytes, back: bytes | None,
             keys: dict[str, str], out_dir: Path, resolution: str,
             views: list[str], tag: str) -> list[dict[str, Any]]:
    """Every view for one arm, anchor first."""
    rows: list[dict[str, Any]] = []
    reference: bytes | None = None

    for view in views:
        sources = [front] + ([back] if back is not None else [])
        if reference is not None and view != "front":
            sources.append(reference)

        if arm.vendor == "gemini":
            result = render_gemini(arm, prompts[view], sources, keys["gemini"],
                                   resolution)
        else:
            result = render_openai(arm, prompts[view], sources, keys["openai"])

        stem = f"{arm.key}__{view}"
        saved, shape = None, None
        if result.ok and result.data:
            ext = ".png" if result.data[:8].startswith(b"\x89PNG") else ".jpg"
            (out_dir / f"{stem}{ext}").write_bytes(result.data)
            saved = f"{stem}{ext}"
            shape = shape_of(result.data)
            if view == "front":
                reference = result.data

        rows.append({
            "arm": arm.key, "prompt": arm.prompt, "vendor": arm.vendor,
            "model": arm.model, "quality": arm.quality, "view": view,
            "ok": result.ok, "error": result.error,
            "seconds": round(result.seconds, 2), "usage": result.usage,
            "cost_usd": cost_of(arm.model, result.usage),
            "bytes": len(result.data) if result.data else 0,
            "file": saved, "shape": shape,
            "prompt_chars": len(prompts[view]),
        })
        status = "ok" if result.ok else f"FAIL {result.error}"
        print(f"  {tag} {stem:36} {result.seconds:6.1f}s "
              f"out={result.usage.get('output_tokens')}  {status}"[:190], flush=True)

        if view == "front" and not result.ok:
            print(f"  {tag} {arm.key}: anchor failed; later views run with no "
                  f"identity reference", flush=True)

    return rows


def run_product(product: str, filing: str, views: list[str], arms: list[Arm],
                people: dict[str, dict[str, Any]], keys: dict[str, str],
                resolution: str, workers: int, dry_run: bool) -> list[dict[str, Any]]:
    folder = ROOT / product
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    person = people[meta["gender"]]

    category = meta["category"] if filing == "real" else meta["gapCategory"]
    sub = meta["subCategory"] if filing == "real" else meta["gapSubCategory"]

    print(f"\n=== {product}  (group {meta['group']}, {meta['gender']}) ===")
    print(f"    filed as {category} / {sub}   mannequinType={meta['mannequinType']}")
    print(f"    hermes class: {classify_garment_type(category, sub)}")
    print(f"    model: {describe(person)}")

    prompts = {
        name: BUILDERS[name](meta, category, sub, views, person)
        for name in sorted({a.prompt for a in arms})
    }

    out_dir = OUT_ROOT / product / filing
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, per_view in prompts.items():
        for view, text in per_view.items():
            (out_dir / f"prompt__{name}__{view}.txt").write_text(text, encoding="utf-8")

    # Everything the report needs that is not a render.
    (out_dir / "context.json").write_text(json.dumps({
        "product": product, "filing": filing, "group": meta["group"],
        "gender": meta["gender"], "category": category, "subCategory": sub,
        "mannequinType": meta["mannequinType"], "bodyType": meta.get("bodyType"),
        "background": meta.get("background"),
        "hermesClass": classify_garment_type(category, sub),
        "personality": {k: v for k, v in person.items() if k != "previewImage"},
        "sources": meta["images"],
        "arms": [
            {"key": a.key, "prompt": a.prompt, "model": a.model,
             "quality": a.quality, "note": a.note} for a in arms
        ],
        "views": views,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    if dry_run:
        for name, per_view in prompts.items():
            for view in views:
                print(f"    prompt {name}/{view}: {len(per_view[view])} chars")
        return []

    front = compress_source((folder / meta["images"]["front"]).read_bytes())
    back = (
        compress_source((folder / meta["images"]["back"]).read_bytes())
        if meta["images"].get("back") else None
    )

    rows: list[dict[str, Any]] = []
    tag = f"[{product[:18]:18}]"
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(run_cell, arm, prompts[arm.prompt], front, back, keys,
                        out_dir, resolution, views, tag): arm.key
            for arm in arms
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                rows.extend(future.result())
            except Exception as exc:  # noqa: BLE001
                print(f"  {tag} cell {futures[future]} raised: {exc}", flush=True)

    for row in rows:
        row.update(product=product, filing=filing, category=category,
                   subCategory=sub, personality=person.get("name"))
    (out_dir / "usage.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )

    ok = sum(1 for r in rows if r["ok"])
    known = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    print(f"    {ok}/{len(rows)} ok, ${sum(known):.4f}")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--product")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--filing", choices=["real", "gap"], default="real")
    ap.add_argument("--arms", default=",".join(a.key for a in ARMS))
    ap.add_argument("--views", default=",".join(DEFAULT_VIEWS))
    ap.add_argument("--resolution", default="2K")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    views = [v.strip() for v in args.views.split(",") if v.strip()]
    arms = [a for a in ARMS if a.key in {x.strip() for x in args.arms.split(",")}]

    e = env()
    people = load_personalities(e["DATABASE_URL"])
    keys = {
        "gemini": (e.get("GEMINI_API_KEY")
                   or e.get("GOOGLE_NANO_BANANA_API_KEY") or "").split(",")[0],
        "openai": e.get("OPENAI_API_KEY", ""),
    }

    if args.all:
        products = sorted(
            p.name for p in ROOT.iterdir()
            if p.is_dir() and not p.name.startswith("_")
            and (p / "meta.json").exists()
        )
    elif args.product:
        products = [args.product]
    else:
        raise SystemExit("pass --product or --all")

    print(f"model library (local DB): "
          + " | ".join(f"{g}: {describe(p)}" for g, p in sorted(people.items())))
    print(f"{len(products)} products x {len(arms)} arms x {len(views)} views "
          f"= {len(products) * len(arms) * len(views)} renders")

    started = time.perf_counter()
    everything: list[dict[str, Any]] = []
    for product in products:
        everything.extend(run_product(
            product, args.filing, views, arms, people, keys,
            args.resolution, args.workers, args.dry_run,
        ))

    if args.dry_run:
        print("\ndry run — nothing called.")
        return

    ok = sum(1 for r in everything if r["ok"])
    known = [r["cost_usd"] for r in everything if r["cost_usd"] is not None]
    print(f"\n{'=' * 60}")
    print(f"{ok}/{len(everything)} renders in "
          f"{(time.perf_counter() - started) / 60:.1f} min")
    print(f"metered total: ${sum(known):.4f}")
    print(f"-> {OUT_ROOT}")


if __name__ == "__main__":
    main()
