"""Gemini evidence layer.

Design rule: **the model never writes a value, it only supplies evidence.**
Every response is forced through a JSON Schema and then re-validated against the
policy before the deterministic resolver is allowed to act on it. Fields listed
under `guardrails.llm_forbidden_fields` are stripped out unconditionally, so the
model cannot set a price even if it tries.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from google import genai
from google.genai import types

from app.models import AttributeVerdict, ProductSnapshot, RRPEvidence, VisionAudit
from app.net import genai_client_args

log = logging.getLogger("hermes.gemini")

_RRP_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "rrp": {"type": ["number", "null"],
                "description": "Original full retail price when new, excluding sales."},
        "currency": {"type": ["string", "null"], "description": "ISO 4217 code."},
        "confidence": {"type": "number", "description": "0.0 to 1.0."},
        "reasoning": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["found", "rrp", "currency", "confidence", "reasoning", "sources"],
}

_VERDICT_ITEM = {
    "type": "object",
    "properties": {
        "field": {"type": "string"},
        "verdict": {"type": "string", "enum": ["confirm", "contradict", "uncertain"]},
        "observed_value": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "evidence": {"type": "string"},
    },
    "required": ["field", "verdict", "observed_value", "confidence", "evidence"],
}

_VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {"type": "array", "items": _VERDICT_ITEM},
        "visible_defects": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["verdicts", "visible_defects", "notes"],
}

_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": _VERDICT_ITEM}},
    "required": ["verdicts"],
}

_BACKGROUND_SCHEMA = {
    "type": "object",
    "properties": {
        "background": {
            "type": "string",
            "enum": ["transparent", "solid_studio", "real_scene"],
        },
        "confidence": {"type": "number", "description": "0.0 to 1.0."},
        "reasoning": {"type": "string"},
    },
    "required": ["background", "confidence", "reasoning"],
}


class GeminiEvidence:
    def __init__(self, api_key: str, pol: dict[str, Any]) -> None:
        # `client_args` rather than a client built here: the SDK constructs its
        # own httpx client, so a custom transport has to be passed in. Empty
        # unless HERMES_IMAGE_FETCH_IPV4 is set — see app/net.py for the 64s
        # IPv6 stall that switch exists for.
        args = genai_client_args()
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(client_args=args) if args else None,
        )
        self.pol = pol
        self.cfg = pol["llm"]
        self.calls = 0
        self.errors: list[str] = []

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _schema_config(schema: dict[str, Any]) -> dict[str, Any]:
        """Build the structured-output config for whichever SDK is installed.

        The field names have moved around across versions:
          - `response_json_schema` takes full JSON Schema (type unions, etc.)
          - `response_schema` takes the older OpenAPI-flavoured subset
          - `response_format` is the Interactions API shape, NOT generate_content
        We probe the installed types rather than assume.
        """
        fields = set(types.GenerateContentConfig.model_fields)
        cfg: dict[str, Any] = {"response_mime_type": "application/json"}
        if "response_json_schema" in fields:
            cfg["response_json_schema"] = schema
        elif "response_schema" in fields:
            cfg["response_schema"] = schema
        else:  # very old SDK — mime type alone is only a hint
            log.warning("SDK exposes no response schema field; JSON is unenforced.")
        return cfg

    @staticmethod
    def _parse(text: str) -> dict[str, Any]:
        """Parse the response, tolerating markdown fences if a model adds them."""
        raw = (text or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.lstrip().lower().startswith("json"):
                raw = raw.lstrip()[4:]
        return json.loads(raw)

    def _generate(self, model: str, contents: Any, schema: dict[str, Any],
                  system: str, grounded: bool = False) -> dict[str, Any] | None:
        config: dict[str, Any] = {"system_instruction": system}
        config.update(self._schema_config(schema))
        if grounded:
            config["tools"] = [{"google_search": {}}]
        try:
            self.calls += 1
            resp = self.client.models.generate_content(
                model=model, contents=contents, config=config,
            )
            return self._parse(resp.text)
        except Exception as exc:  # noqa: BLE001 — evidence is best-effort
            msg = f"{model}: {type(exc).__name__}: {exc}"
            log.warning("gemini call failed (%s)", msg)
            self.errors.append(msg)
            return None

    def _strip_forbidden(self, verdicts: list[dict[str, Any]]) -> list[AttributeVerdict]:
        blocked = set(self.pol["guardrails"]["llm_forbidden_fields"])
        out = []
        for v in verdicts:
            if v.get("field") in blocked:
                log.info("dropped LLM verdict on guarded field %s", v.get("field"))
                continue
            try:
                out.append(AttributeVerdict.model_validate(v))
            except Exception:  # noqa: BLE001
                continue
        return out

    # ------------------------------------------------------------------ calls
    def ground_rrp(self, p: ProductSnapshot, currency: str) -> RRPEvidence:
        """Search-grounded lookup of the item's ORIGINAL retail price.

        This is the anchor the whole price calculation hangs off. We ask only for
        RRP — never for a resale price — because resale price is ours to compute.
        """
        descriptor = " ".join(filter(None, [
            p.brand, p.model, p.color, p.material,
            p.subcategory or p.category, f"size {p.size}" if p.size else None,
        ]))
        prompt = (
            f"Find the original recommended retail price (RRP / MSRP) for this "
            f"garment when it was sold new: {descriptor}.\n"
            f"Report the price in {currency}. Use the brand's own site or major "
            f"retailers. Ignore second-hand, outlet and sale prices — we want the "
            f"full new price. If the exact model cannot be identified, report the "
            f"typical new price for that brand's equivalent product line and lower "
            f"your confidence accordingly. If you cannot establish it at all, set "
            f"found=false."
        )
        raw = self._generate(
            model=self.cfg["model_reasoning"],
            contents=prompt,
            schema=_RRP_SCHEMA,
            system="You are a retail pricing researcher. Cite every figure you "
                   "report. Never guess without lowering confidence.",
            grounded=True,
        )
        if not raw:
            return RRPEvidence()
        try:
            ev = RRPEvidence.model_validate(raw)
        except Exception:  # noqa: BLE001
            return RRPEvidence()
        # Sanity gate: a garment RRP outside this range is almost certainly noise.
        if ev.rrp is not None and not (1 <= ev.rrp <= 20000):
            return RRPEvidence(found=False, reasoning="RRP outside plausible range")
        return ev

    def audit_images(self, p: ProductSnapshot, fields: dict[str, Any]) -> VisionAudit:
        """Check the structured attributes against the actual product photos."""
        images = self._fetch_images(p.images[: self.cfg["max_images"]])
        if not images:
            return VisionAudit()

        parts: list[Any] = list(images)
        parts.append(
            "Here is what our catalogue claims about the garment in these photos:\n"
            + json.dumps(fields, indent=2)
            + "\n\nFor each field, decide whether the photos confirm it, contradict "
              "it, or are inconclusive. Judge only what is actually visible. If a "
              "brand label, wash, weave or fit is not legible, say 'uncertain' "
              "rather than inferring. Also list any wear or damage you can see "
              "(fading, snags, holes, stains, pilling, missing hardware)."
        )
        raw = self._generate(
            model=self.cfg["model_fast"], contents=parts, schema=_VISION_SCHEMA,
            system="You are a garment quality inspector. You are conservative: "
                   "you never claim to see something you cannot clearly see.",
        )
        if not raw:
            return VisionAudit()
        return VisionAudit(
            verdicts=self._strip_forbidden(raw.get("verdicts", [])),
            visible_defects=raw.get("visible_defects", []),
            notes=raw.get("notes", ""),
        )

    def audit_description(self, p: ProductSnapshot,
                          fields: dict[str, Any]) -> list[AttributeVerdict]:
        """Find claims in the marketing copy that contradict the structured data."""
        if not (p.description or "").strip():
            return []
        prompt = (
            "Structured product data:\n" + json.dumps(fields, indent=2)
            + "\n\nMarketing description:\n" + p.description
            + "\n\nList every structured field the description contradicts. Report "
              "'confirm' only for fields the description explicitly supports, and "
              "omit fields the description never mentions."
        )
        raw = self._generate(
            model=self.cfg["model_fast"], contents=prompt, schema=_TEXT_SCHEMA,
            system="You check product copy against a database record and report "
                   "factual disagreements. Be literal.",
        )
        if not raw:
            return []
        return self._strip_forbidden(raw.get("verdicts", []))

    def classify_background(self, image: bytes, mime: str = "image/jpeg",
                            ) -> tuple[str, float, str]:
        """Is there still a background behind this garment?

        The tie-breaker for the one case the pixel heuristic genuinely cannot
        call: a garment photographed against a plain wall produces the same border
        statistic as one on a studio sweep, and the difference matters — the first
        needs the segmenter, the second is finished. Only images in that middle
        band get here, so this costs a call per genuinely doubtful picture rather
        than one per picture.

        Returns ("unknown", 0.0, reason) on any failure. The caller keeps the
        pixel verdict in that case, so a Gemini outage degrades the answer's
        confidence rather than removing it.
        """
        parts: list[Any] = [
            types.Part.from_bytes(data=image, mime_type=mime),
            "Look ONLY at what is behind the product, not at the product itself.\n"
            "  transparent  — no background at all: a checkerboard, or the subject "
            "cut out against nothing.\n"
            "  solid_studio — a deliberate flat colour, seamless sweep or studio "
            "backdrop. A plain painted wall with no objects, edges or floor line "
            "counts as this.\n"
            "  real_scene   — an actual place: furniture, shelving, a floor, a "
            "doorway, hangers, clutter, a visible corner where two surfaces meet.\n"
            "If you can see where the wall meets the floor, that is real_scene. "
            "Report your confidence honestly; this decides whether an automated "
            "pipeline reprocesses the image.",
        ]
        raw = self._generate(
            model=self.cfg["model_fast"], contents=parts,
            schema=_BACKGROUND_SCHEMA,
            system="You inspect product photography for an e-commerce catalogue "
                   "and report only what is visibly there.",
        )
        if not raw:
            return "unknown", 0.0, "vision call failed"
        verdict = str(raw.get("background") or "unknown")
        try:
            confidence = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return verdict, confidence, str(raw.get("reasoning") or "")

    # ------------------------------------------------------------------ utils
    def _fetch_images(self, urls: list[str]) -> list[types.Part]:
        parts: list[types.Part] = []
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            for url in urls:
                try:
                    r = client.get(url)
                    r.raise_for_status()
                    mime = r.headers.get("content-type", "image/jpeg").split(";")[0]
                    if not mime.startswith("image/"):
                        continue
                    parts.append(types.Part.from_bytes(data=r.content, mime_type=mime))
                except Exception as exc:  # noqa: BLE001
                    msg = f"image fetch failed ({url[:60]}): {type(exc).__name__}"
                    log.warning(msg)
                    self.errors.append(msg)
        return parts