"""One Auto Approval run → one HTML page a person can scan.

    python scripts/run_grid.py --latest                       # newest run, any tenant
    python scripts/run_grid.py --latest --tenant BOAS
    python scripts/run_grid.py --run 7c1e…                    # a specific run
    python scripts/run_grid.py --sku CBOA-006107              # the newest run that judged this SKU
    python scripts/run_grid.py --latest --out review.html --open

WHY THIS EXISTS. The run tables answer any question you can phrase in SQL, and
the Runs screen answers the ones it was built for. Neither shows a reviewer
sixty products at once with the picture the shopper will see next to the reason
the agent held it. The auditor had that — `visual_grid.py`, a contact sheet of
a Shopify collection with the model's flags — and it was the artefact people
actually opened after a run. This is the same sheet, drawn from Hermes' own run
record rather than from scanning the store: lead render, SKU, verdict, outcome
code, the blocking rules, the pre-flight problems, every step's one-line note,
and the edit link.

WHAT IT READS. `AutoApprovalRun` for the header and counters, `AutoApprovalRunProduct`
for every verdict, `ProductMedia` for the render to show. Read-only, one
connection, no vnyx-api, no Shopify. The picture is the AI front render when the
product has one — that is what the gate judged — else the first gallery image.

WORST FIRST. Held, then failed, then verified, then cancelled, then whatever is
still queued: the page opens on the products that need a person.

The output is a single self-contained file (inline CSS and script; images by
URL from the public bucket) so it can be attached to a message or dropped in a
shared folder. It lists SKUs, titles and edit URLs — tenant data — which is why
it lands in `reports/generated/`, a directory git ignores.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import product_audit  # noqa: E402
from app.config import settings  # noqa: E402

RUN_SQL = """
SELECT r.id::text, r."tenantId"::text, t.name, r.source::text, r.status::text,
       r."configSnapshot", r."requestParams",
       r."totalProducts", r."verifiedCount", r."heldCount", r."failedCount",
       r."cancelledCount", r."approvedCount",
       r."startedAt", r."completedAt", r."completionReason", r."reportUrl"
  FROM "AutoApprovalRun" r
  JOIN "Tenant" t ON t.id = r."tenantId"
 WHERE {where}
 ORDER BY r."startedAt" DESC
 LIMIT 1
"""

ROWS_SQL = """
SELECT p.id::text, p."productId"::text, p."productSku", p."productTitle",
       p.status::text, p.outcome, p.reason, p.error,
       p."blockingRules", p."preflightProblems", p.steps, p.deltas,
       p.approved, p.attempts, p."durationMs", p."completedAt",
       pr."currentStage"::text, pr.images,
       (SELECT m.url FROM "ProductMedia" m
         WHERE m."productId" = p."productId" AND m."isCurrent" AND m."deletedAt" IS NULL
           AND m."mediaType" = 'IMAGE' AND m.view::text IN ('AI_FRONT', 'AI_FRONT_34')
         ORDER BY CASE m.view::text WHEN 'AI_FRONT' THEN 0 ELSE 1 END, m.position
         LIMIT 1) AS render
  FROM "AutoApprovalRunProduct" p
  LEFT JOIN "Product" pr ON pr.id = p."productId"
 WHERE p."runId" = %(run)s::uuid
 ORDER BY CASE p.status::text
            WHEN 'HELD_FOR_HUMAN' THEN 0 WHEN 'FAILED' THEN 1
            WHEN 'VERIFIED' THEN 2 WHEN 'CANCELLED' THEN 3 ELSE 4 END,
          p."completedAt" DESC NULLS LAST, p."productSku"
"""

RUN_COLUMNS = ("id", "tenant_id", "tenant", "source", "status", "config", "params",
               "total", "verified", "held", "failed", "cancelled", "approved",
               "started_at", "completed_at", "completion_reason", "report_url")
ROW_COLUMNS = ("id", "product_id", "sku", "title", "status", "outcome", "reason", "error",
               "blocking_rules", "preflight_problems", "steps", "deltas", "approved",
               "attempts", "duration_ms", "completed_at", "stage", "images", "render")

# Status → (label, css class). The four terminal statuses plus the two a page
# drawn mid-run will meet.
STATUS = {
    "VERIFIED": ("verified", "ok"),
    "HELD_FOR_HUMAN": ("held", "held"),
    "FAILED": ("failed", "bad"),
    "CANCELLED": ("cancelled", "off"),
    "QUEUED": ("queued", "wait"),
    "IN_PROGRESS": ("running", "wait"),
}


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def fetch(dsn: str, *, run_id: str | None = None, tenant: str | None = None,
          sku: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The run header and its product rows. Raises LookupError when none matches."""
    where, params = ["true"], {}
    if run_id:
        where.append("r.id = %(run)s::uuid")
        params["run"] = run_id
    if tenant:
        where.append("t.name ILIKE %(tenant)s")
        params["tenant"] = tenant
    if sku:
        where.append('EXISTS (SELECT 1 FROM "AutoApprovalRunProduct" q '
                     ' WHERE q."runId" = r.id AND upper(q."productSku") = upper(%(sku)s))')
        params["sku"] = sku
    with product_audit.connect(dsn, read_only=True) as conn, conn.cursor() as cur:
        cur.execute(RUN_SQL.format(where=" AND ".join(where)), params)
        head = cur.fetchone()
        if head is None:
            raise LookupError("no run matches")
        run = dict(zip(RUN_COLUMNS, head))
        cur.execute(ROWS_SQL, {"run": run["id"]})
        rows = [dict(zip(ROW_COLUMNS, r)) for r in cur.fetchall()]
    return run, rows


def pick_image(row: dict[str, Any]) -> tuple[str | None, str]:
    """The picture to show and what it is: the judged render, else the lead."""
    if row.get("render"):
        return str(row["render"]), "render"
    images = row.get("images") or []
    if images:
        return str(images[0]), "lead"
    return None, "none"


def edit_url(product_id: str, tenant_id: str | None) -> str:
    base = product_audit.DEFAULT_APP_BASE.rstrip("/")
    return f"{base}/product/{product_id}/edit" + (f"?tenantId={tenant_id}" if tenant_id else "")


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _when(ts: Any) -> str:
    if not ts:
        return "—"
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M")
    return _e(ts)


def _duration(ms: Any) -> str:
    try:
        s = int(ms) / 1000.0
    except (TypeError, ValueError):
        return ""
    return f"{s:.0f}s" if s < 90 else f"{s / 60:.1f} min"


def _mode(run: dict[str, Any]) -> str:
    snap = run.get("config") if isinstance(run.get("config"), dict) else {}
    mode = str(snap.get("mode") or "").upper()
    if mode == "SHADOW" and snap.get("shadowWritesRepairs"):
        return "SHADOW + repairs"
    return mode or "—"


def _steps_html(steps: Any) -> str:
    if not isinstance(steps, list) or not steps:
        return '<p class="muted">No step record.</p>'
    items = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        name = _e(s.get("step"))
        if s.get("ran"):
            mark = "✕" if s.get("ok") is False else "✓"
            cls = "bad" if s.get("ok") is False else "ok"
            note = _e(s.get("note") or "")
            secs = s.get("seconds")
            tail = f' <span class="muted">{_e(f"{secs:.0f}s")}</span>' if isinstance(secs, (int, float)) and secs >= 1 else ""
            items.append(f'<li><b class="{cls}">{mark}</b> <b>{name}</b> {note}{tail}</li>')
        else:
            items.append(f'<li class="skip"><b>–</b> <b>{name}</b> <span class="muted">{_e(s.get("why") or "skipped")}</span></li>')
    return "<ul class=\"steps\">" + "".join(items) + "</ul>"


def _card(row: dict[str, Any], tenant_id: str | None) -> str:
    label, cls = STATUS.get(str(row.get("status")), (str(row.get("status") or "?").lower(), "wait"))
    img, kind = pick_image(row)
    rules = [str(r) for r in (row.get("blocking_rules") or [])]
    problems = [str(p) for p in (row.get("preflight_problems") or [])]
    search = " ".join([row.get("sku") or "", row.get("title") or "", row.get("outcome") or "",
                       row.get("reason") or "", *rules, *problems]).lower()
    picture = (
        f'<img loading="lazy" src="{_e(img)}" alt="">' if img
        else '<div class="noimg">no image</div>'
    )
    chips = "".join(f'<span class="chip rule">{_e(r)}</span>' for r in rules)
    chips += "".join(f'<span class="chip pre">{_e(p)}</span>' for p in problems)
    approved = '<span class="chip go">approved</span>' if row.get("approved") else ""
    error = f'<p class="err">{_e(str(row["error"])[:300])}</p>' if row.get("error") else ""
    return f"""
<article class="card {cls}" data-status="{_e(row.get('status'))}" data-approved="{'1' if row.get('approved') else '0'}" data-search="{_e(search)}">
  <a class="pic" href="{_e(img or '#')}" target="_blank" rel="noopener">{picture}<span class="kind">{_e(kind)}</span></a>
  <div class="body">
    <div class="top"><span class="status">{_e(label)}</span>{approved}<span class="stage muted">{_e(row.get('stage') or '')}</span></div>
    <h3><span class="sku">{_e(row.get('sku'))}</span> {_e(row.get('title') or '')}</h3>
    <p class="outcome">{_e(row.get('outcome') or '—')}</p>
    <p class="reason">{_e(row.get('reason') or '')}</p>
    {error}
    <div class="chips">{chips}</div>
    <details><summary>steps <span class="muted">{_e(_duration(row.get('duration_ms')))}</span></summary>{_steps_html(row.get('steps'))}</details>
    <div class="foot">
      <a href="{_e(edit_url(row['product_id'], tenant_id))}" target="_blank" rel="noopener">Edit in vnyx</a>
      <span class="muted">{_when(row.get('completed_at'))}{' · attempt ' + _e(row['attempts']) if (row.get('attempts') or 0) > 1 else ''}</span>
    </div>
  </div>
</article>"""


def render(run: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """The whole page. Pure: no database, no clock beyond the footer stamp."""
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[str(r.get("status"))] = by_status.get(str(r.get("status")), 0) + 1
    approved = sum(1 for r in rows if r.get("approved"))
    cards = "\n".join(_card(r, run.get("tenant_id")) for r in rows)
    filters = "".join(
        f'<button type="button" data-filter="{_e(status)}" class="{cls}">'
        f'{_e(label)} <b>{by_status.get(status, 0)}</b></button>'
        for status, (label, cls) in STATUS.items() if by_status.get(status)
    )
    title = f"{run.get('tenant') or 'run'} · {str(run.get('id') or '')[:8]}"
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report = (f' · <a href="{_e(run["report_url"])}" target="_blank" rel="noopener">xlsx report</a>'
              if run.get("report_url") else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} — Auto Approval run</title>
<style>
:root {{ --bg:#f6f5f2; --card:#fff; --ink:#1c1b19; --mute:#6f6b63; --line:#e3e0d9;
        --ok:#2f7d4f; --held:#b9791a; --bad:#b3362d; --off:#8a867e; --wait:#3b6ea8; --go:#1f6f8b; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#17171a; --card:#212126; --ink:#ecebe7; --mute:#a09c94; --line:#33333a; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; padding:20px 16px 48px; background:var(--bg); color:var(--ink);
       font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif }}
header {{ max-width:1400px; margin:0 auto 18px }}
header h1 {{ margin:0 0 4px; font-size:22px; font-weight:650 }}
header .facts {{ color:var(--mute); display:flex; flex-wrap:wrap; gap:6px 18px; font-variant-numeric:tabular-nums }}
header a {{ color:inherit }}
.bar {{ max-width:1400px; margin:0 auto 14px; display:flex; flex-wrap:wrap; gap:8px; align-items:center }}
.bar button {{ border:1px solid var(--line); background:var(--card); color:var(--ink); padding:6px 10px;
              border-radius:999px; cursor:pointer; font:inherit }}
.bar button b {{ font-weight:650; margin-left:4px }}
.bar button.on {{ outline:2px solid var(--ink); outline-offset:1px }}
.bar button.ok b {{ color:var(--ok) }} .bar button.held b {{ color:var(--held) }} .bar button.bad b {{ color:var(--bad) }}
.bar button.off b {{ color:var(--off) }} .bar button.wait b {{ color:var(--wait) }}
.bar input {{ flex:1 1 220px; min-width:160px; border:1px solid var(--line); border-radius:8px; padding:7px 10px;
             background:var(--card); color:var(--ink); font:inherit }}
.bar label {{ color:var(--mute); display:flex; gap:6px; align-items:center }}
.grid {{ max-width:1400px; margin:0 auto; display:grid; gap:14px;
        grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)) }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden;
        display:flex; flex-direction:column; border-top:4px solid var(--off) }}
.card.ok {{ border-top-color:var(--ok) }} .card.held {{ border-top-color:var(--held) }}
.card.bad {{ border-top-color:var(--bad) }} .card.wait {{ border-top-color:var(--wait) }}
.card[hidden] {{ display:none }}
.pic {{ position:relative; display:block; aspect-ratio:3/4; background:#ecebe7; max-width:100% }}
.pic img {{ width:100%; height:100%; object-fit:cover; display:block }}
.noimg {{ height:100%; display:grid; place-items:center; color:var(--mute) }}
.kind {{ position:absolute; left:8px; bottom:8px; font-size:11px; letter-spacing:.04em; text-transform:uppercase;
        background:rgba(0,0,0,.55); color:#fff; padding:2px 6px; border-radius:4px }}
.body {{ padding:10px 12px 12px; display:flex; flex-direction:column; gap:6px }}
.top {{ display:flex; gap:8px; align-items:center; font-size:12px; text-transform:uppercase; letter-spacing:.04em }}
.top .status {{ font-weight:650 }}
.card.ok .status {{ color:var(--ok) }} .card.held .status {{ color:var(--held) }}
.card.bad .status {{ color:var(--bad) }} .card.wait .status {{ color:var(--wait) }} .card.off .status {{ color:var(--off) }}
.top .stage {{ margin-left:auto; text-transform:none; letter-spacing:0 }}
h3 {{ margin:0; font-size:14px; font-weight:500; line-height:1.35 }}
h3 .sku {{ font-weight:650; font-variant-numeric:tabular-nums }}
.outcome {{ margin:0; font-family:ui-monospace,Consolas,monospace; font-size:12.5px }}
.reason {{ margin:0; color:var(--mute); display:-webkit-box; -webkit-line-clamp:4; -webkit-box-orient:vertical; overflow:hidden }}
.err {{ margin:0; color:var(--bad); font-size:12px; overflow-wrap:anywhere }}
.chips {{ display:flex; flex-wrap:wrap; gap:4px }}
.chip {{ font-size:11px; padding:2px 7px; border-radius:999px; border:1px solid var(--line); font-variant-numeric:tabular-nums }}
.chip.rule {{ border-color:var(--bad); color:var(--bad) }} .chip.pre {{ border-color:var(--held); color:var(--held) }}
.chip.go {{ border-color:var(--go); color:var(--go); text-transform:none; letter-spacing:0 }}
details summary {{ cursor:pointer; color:var(--mute); font-size:12.5px }}
.steps {{ margin:6px 0 0; padding:0; list-style:none; font-size:12.5px; display:flex; flex-direction:column; gap:3px }}
.steps li {{ overflow-wrap:anywhere }} .steps li.skip {{ color:var(--mute) }}
.steps b.ok {{ color:var(--ok) }} .steps b.bad {{ color:var(--bad) }}
.foot {{ margin-top:auto; padding-top:6px; display:flex; justify-content:space-between; gap:8px; font-size:12px }}
.foot a {{ color:var(--go) }}
.muted {{ color:var(--mute) }}
footer {{ max-width:1400px; margin:28px auto 0; color:var(--mute); font-size:12px }}
</style></head>
<body>
<header>
  <h1>{_e(run.get('tenant'))} · Auto Approval run <span class="muted">{_e(str(run.get('id') or '')[:8])}</span></h1>
  <div class="facts">
    <span>{_e(run.get('source'))} · {_e(_mode(run))} · {_e(run.get('status'))}</span>
    <span>started {_when(run.get('started_at'))}</span>
    <span>finished {_when(run.get('completed_at'))}{' · ' + _e(run['completion_reason']) if run.get('completion_reason') else ''}</span>
    <span>{len(rows)} product{'' if len(rows) == 1 else 's'} on this page · {approved} approved{report}</span>
  </div>
</header>
<div class="bar">
  <button type="button" data-filter="" class="on">all <b>{len(rows)}</b></button>
  {filters}
  <input id="q" type="search" placeholder="filter by SKU, title, outcome, rule…" aria-label="filter">
  <label><input id="approved-only" type="checkbox"> approved only</label>
</div>
<section class="grid" id="grid">
{cards}
</section>
<footer>Generated {generated} by scripts/run_grid.py from the run record. Images are the AI front render the gate judged, else the gallery lead. Cards are ordered worst first: held, failed, verified, cancelled.</footer>
<script>
(function () {{
  var status = "", q = "", approvedOnly = false;
  var cards = Array.prototype.slice.call(document.querySelectorAll(".card"));
  var buttons = Array.prototype.slice.call(document.querySelectorAll(".bar button"));
  function apply() {{
    cards.forEach(function (c) {{
      var show = (!status || c.dataset.status === status)
              && (!approvedOnly || c.dataset.approved === "1")
              && (!q || c.dataset.search.indexOf(q) !== -1);
      c.hidden = !show;
    }});
  }}
  buttons.forEach(function (b) {{
    b.addEventListener("click", function () {{
      status = b.dataset.filter || "";
      buttons.forEach(function (x) {{ x.classList.toggle("on", x === b); }});
      apply();
    }});
  }});
  document.getElementById("q").addEventListener("input", function (e) {{ q = e.target.value.trim().toLowerCase(); apply(); }});
  document.getElementById("approved-only").addEventListener("change", function (e) {{ approvedOnly = e.target.checked; apply(); }});
}})();
</script>
</body></html>
"""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", "--dsn", dest="db")
    pick = ap.add_mutually_exclusive_group()
    pick.add_argument("--run", help="run id (uuid)")
    pick.add_argument("--latest", action="store_true", help="the newest run (default)")
    pick.add_argument("--sku", help="the newest run that judged this SKU")
    ap.add_argument("--tenant", help="restrict --latest / --sku to one tenant (name, case-insensitive)")
    ap.add_argument("--out", help="where to write. Default reports/generated/run-<id8>.html")
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    args = ap.parse_args(argv)

    dsn = args.db or os.getenv("DATABASE_URL") or settings().database_url
    if not dsn:
        sys.exit("No --db, and no DATABASE_URL.")

    try:
        run, rows = fetch(dsn, run_id=args.run, tenant=args.tenant, sku=args.sku)
    except LookupError:
        sys.exit("No run matches" + (f" --run {args.run}" if args.run else "")
                 + (f" for tenant {args.tenant!r}" if args.tenant else "")
                 + (f" with SKU {args.sku}" if args.sku else "") + ".")

    out = Path(args.out) if args.out else product_audit.REPORT_DIR / f"run-{run['id'][:8]}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(run, rows), encoding="utf-8")

    counts = {s: sum(1 for r in rows if r["status"] == s) for s in STATUS}
    print(f"{run['tenant']} · run {run['id']} · {run['source']} · {run['status']}")
    print("  " + " · ".join(f"{STATUS[s][0]} {n}" for s, n in counts.items() if n)
          + f" · approved {sum(1 for r in rows if r['approved'])}")
    print(f"  html: {out}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
