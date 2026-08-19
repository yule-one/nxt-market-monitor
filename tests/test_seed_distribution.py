from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src import seed_distribution


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.payload), chunk_size):
            yield self.payload[offset : offset + chunk_size]


def _manifest(root: Path, payload: bytes) -> None:
    manifest = {
        "schema_version": 1,
        "latest_trade_date": "2026-08-18",
        "release_tag": "daily-market-data",
        "assets": {
            "history": {
                "name": "history.db.gz",
                "url": "https://example.test/history.db.gz",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        },
    }
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "daily_seed_manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def test_uses_bundled_seed_when_it_matches_manifest(tmp_path: Path, monkeypatch) -> None:
    payload = b"matching-seed"
    bundled = tmp_path / "data" / "history.db.gz"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(payload)
    _manifest(tmp_path, payload)
    monkeypatch.setattr(
        seed_distribution.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("downloaded")),
    )
    resolved = seed_distribution.resolve_deployed_seed(
        tmp_path,
        bundled,
        "history",
    )

    assert resolved == bundled.resolve()


def test_downloads_and_caches_release_seed(tmp_path: Path, monkeypatch) -> None:
    remote_payload = b"new-release-seed"
    bundled = tmp_path / "data" / "history.db.gz"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"old-bundled-seed")
    _manifest(tmp_path, remote_payload)
    monkeypatch.setattr(
        seed_distribution.requests,
        "get",
        lambda *_args, **_kwargs: _Response(remote_payload),
    )
    resolved = seed_distribution.resolve_deployed_seed(
        tmp_path,
        bundled,
        "history",
    )

    assert resolved != bundled.resolve()
    assert resolved.read_bytes() == remote_payload
    assert resolved.parent.name == "nxt-market-monitor-seed-cache"


def test_falls_back_to_bundled_seed_when_download_hash_is_wrong(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bundled = tmp_path / "data" / "history.db.gz"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"old-bundled-seed")
    _manifest(tmp_path, b"expected-release-seed")
    monkeypatch.setattr(
        seed_distribution.requests,
        "get",
        lambda *_args, **_kwargs: _Response(b"corrupted-download"),
    )
    resolved = seed_distribution.resolve_deployed_seed(
        tmp_path,
        bundled,
        "history",
    )

    assert resolved == bundled.resolve()
