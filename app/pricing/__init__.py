"""Resale price lookup — what an item actually resells for, used.

Ported from the BOAS handoff of 5 Oct 2026 (`vnyx_price_lookup`, Balazs).
`resale_rules` is the pricing maths and nothing else; `resale_lookup` is the
Gemini + Google Search side. `scripts/resale_price.py` drives both for a list
of SKUs and writes nothing to the database.
"""
