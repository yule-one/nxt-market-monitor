from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


LOGGER = logging.getLogger(__name__)
MANIFEST_NAME = "daily_seed_manifest.json"


@dataclass(frozen=True)
class RemoteSeedAsset:
    name: str
    url: str
    sha256: str
    size: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _matches(path: Path, asset: RemoteSeedAsset) -> bool:
    return (
        path.exists()
        and path.is_file()
        and path.stat().st_size == asset.size
        and _sha256(path) == asset.sha256
    )


def _asset_from_manifest(
    project_root: Path,
    asset_key: str,
) -> RemoteSeedAsset | None:
    manifest_path = project_root / "data" / MANIFEST_NAME
    if not manifest_path.exists():
        return None
    try:
        payload: dict[str, Any] = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
        raw = payload["assets"][asset_key]
        asset = RemoteSeedAsset(
            name=str(raw["name"]),
            url=str(raw["url"]),
            sha256=str(raw["sha256"]).lower(),
            size=int(raw["size"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.warning("배포 DB manifest를 읽지 못했습니다: %s", exc)
        return None
    if (
        not asset.url.startswith("https://")
        or len(asset.sha256) != 64
        or any(character not in "0123456789abcdef" for character in asset.sha256)
        or asset.size <= 0
    ):
        LOGGER.warning("배포 DB manifest의 %s 항목이 올바르지 않습니다.", asset_key)
        return None
    return asset


def _download(asset: RemoteSeedAsset, destination: Path) -> None:
    temporary_path: Path | None = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{asset.name}-",
            suffix=".tmp",
            delete=False,
        ) as target:
            temporary_path = Path(target.name)
            with requests.get(
                asset.url,
                stream=True,
                allow_redirects=True,
                timeout=(10, 180),
            ) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        target.write(chunk)
        if temporary_path is None or not _matches(temporary_path, asset):
            raise RuntimeError(f"다운로드한 {asset.name}의 크기 또는 해시가 다릅니다.")
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def resolve_deployed_seed(
    project_root: Path,
    bundled_seed: Path,
    asset_key: str,
) -> Path:
    """최신 Release seed를 검증해 반환하고 실패 시 번들 seed로 폴백합니다."""

    project_root = project_root.resolve()
    bundled_seed = bundled_seed.resolve()
    asset = _asset_from_manifest(project_root, asset_key)
    if asset is None or _matches(bundled_seed, asset):
        return bundled_seed
    cache_path = (
        Path(tempfile.gettempdir())
        / "nxt-market-monitor-seed-cache"
        / f"{asset_key}-{asset.sha256[:16]}.db.gz"
    )
    if _matches(cache_path, asset):
        return cache_path
    try:
        _download(asset, cache_path)
        return cache_path
    except Exception as exc:
        LOGGER.warning(
            "최신 %s 배포 DB 다운로드에 실패해 저장소 내 seed를 사용합니다: %s",
            asset_key,
            exc,
        )
        return bundled_seed
