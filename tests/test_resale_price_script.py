"""scripts/resale_price.py refuses any database that is not on this machine."""
from __future__ import annotations

import pytest

from scripts import resale_price as cli


@pytest.mark.parametrize("dsn", [
    "postgresql://u:p@localhost:5433/vnyx-dev-2",
    "postgresql://u:p@127.0.0.1:5433/vnyx-dev-2",
])
def test_local_database_is_accepted(monkeypatch, dsn):
    monkeypatch.setattr(cli.db, "dsn", lambda: dsn)
    got, where = cli.local_dsn()
    assert got == dsn and where.endswith("/vnyx-dev-2")


@pytest.mark.parametrize("dsn", [
    "postgresql://u:p@34.7.102.18:5432/vnyx-prod",
    "postgresql://u:p@db.example.com:5432/vnyx-dev",
])
def test_a_remote_database_is_refused(monkeypatch, dsn):
    monkeypatch.setattr(cli.db, "dsn", lambda: dsn)
    with pytest.raises(SystemExit) as exc:
        cli.local_dsn()
    assert "REFUSED" in str(exc.value) and "u:p" not in str(exc.value)


def test_prod_dsn_reads_the_commented_line_and_drops_prisma_schema(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=postgresql://u:p@localhost:5432/vnyx_local\n"
                   "# DATABASE_URL=postgresql://u:p@10.0.0.9:5432/vnyx-prod?schema=public\n")
    monkeypatch.delenv("VNYX_PROD_DATABASE_URL", raising=False)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setenv("VNYX_API_DIR", str(tmp_path / "nowhere"))
    dsn, where = cli.prod_dsn()
    assert dsn == "postgresql://u:p@10.0.0.9:5432/vnyx-prod"
    assert where == "10.0.0.9:5432/vnyx-prod"


def test_the_photo_log_names_what_each_picture_is():
    from app.pricing import resale_lookup as RL
    item = RL.item_from_payload({
        "record": {"sku": "X-1", "images": ["https://r2/ai-front.png"]},
        "media": [{"url": "https://r2/front.jpg", "view": "FRONT", "processing": "RAW", "isCurrent": True},
                  {"url": "https://r2/label.jpg", "view": "LABEL", "processing": "RAW", "isCurrent": True}],
    })
    assert item["photo_labels"]["https://r2/front.jpg"].startswith("FRONT · original photo")
    assert item["photo_labels"]["https://r2/label.jpg"] == "LABEL · care label photo"
    gallery_only = RL.item_from_payload({"record": {"sku": "X-2", "images": ["https://r2/ai-front.png"]},
                                         "media": []})
    assert "may be an AI render" in gallery_only["photo_labels"]["https://r2/ai-front.png"]
