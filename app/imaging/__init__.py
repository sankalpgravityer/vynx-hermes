"""Pixel-level imagery checks and on-model generation.

Everything in here does I/O — downloads an image, or calls the generation model —
which is what separates it from `app/rules/imagery.py`. The rule group is pure
arithmetic over columns and runs on every product of a review-queue page; these
run only on the single-product endpoints, where one operator is waiting on one
answer.
"""
