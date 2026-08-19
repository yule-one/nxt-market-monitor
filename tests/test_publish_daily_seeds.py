from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts import publish_daily_seeds


def _database(path: Path, table: str, trade_date: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(f'CREATE TABLE "{table}" (trade_date TEXT)')
        connection.execute(
            f'INSERT INTO "{table}" (trade_date) VALUES (?)',
            (trade_date,),
        )


def test_latest_trade_date_requires_matching_database_coverage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _database(
        tmp_path / "data" / "history.db",
        "daily_market_metrics",
        "2026-08-18",
    )
    _database(
        tmp_path / "data" / "krx_listed_history.db",
        "krx_listed_daily",
        "2026-08-18",
    )
    monkeypatch.setattr(publish_daily_seeds, "PROJECT_ROOT", tmp_path)

    assert publish_daily_seeds._latest_trade_date() == "2026-08-18"


def test_same_release_content_ignores_publish_timestamp() -> None:
    assets = {
        "history": {
            "name": "history.db.gz",
            "url": "https://example.test/history.db.gz",
            "sha256": "a" * 64,
            "size": 123,
        }
    }
    existing = {
        "schema_version": 1,
        "latest_trade_date": "2026-08-18",
        "published_at": "2026-08-19T08:00:00+09:00",
        "release_tag": "daily-market-data",
        "assets": assets,
    }
    candidate = {
        **existing,
        "published_at": "2026-08-19T09:00:00+09:00",
    }

    assert publish_daily_seeds._same_release_content(existing, candidate) is True


def test_manifest_excludes_local_asset_paths() -> None:
    manifest = publish_daily_seeds._manifest_payload(
        "2026-08-18",
        {
            "history": {
                "name": "history.db.gz",
                "url": "https://example.test/history.db.gz",
                "sha256": "b" * 64,
                "size": 456,
                "path": Path("private-local-path"),
            }
        },
    )

    assert "path" not in manifest["assets"]["history"]


def test_publisher_skips_build_when_latest_trade_date_is_already_deployed(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        publish_daily_seeds,
        "_latest_trade_date",
        lambda: "2026-08-18",
    )
    monkeypatch.setattr(publish_daily_seeds, "_repository_name", lambda: "o/r")
    monkeypatch.setattr(
        publish_daily_seeds,
        "_remote_manifest",
        lambda: {"latest_trade_date": "2026-08-18"},
    )
    monkeypatch.setattr(
        publish_daily_seeds,
        "_run",
        lambda *_args, **_kwargs: type("Result", (), {"stdout": ""})(),
    )
    monkeypatch.setattr(
        publish_daily_seeds,
        "_build_assets",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("built")),
    )

    assert publish_daily_seeds.publish_daily_seeds() is False
