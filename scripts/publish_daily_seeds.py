from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KST = ZoneInfo("Asia/Seoul")
RELEASE_TAG = "daily-market-data"
ASSET_SPECS = {
    "history": {
        "name": "history.db.gz",
        "builder": PROJECT_ROOT / "scripts" / "build_history_seed.py",
        "source": PROJECT_ROOT / "data" / "history.db",
    },
    "krx_listed_history": {
        "name": "krx_listed_history.db.gz",
        "builder": PROJECT_ROOT / "scripts" / "build_krx_listed_history_seed.py",
        "source": PROJECT_ROOT / "data" / "krx_listed_history.db",
    },
}


def _logger() -> logging.Logger:
    logger = logging.getLogger("daily-seed-publish")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    log_path = PROJECT_ROOT / "data" / "daily_seed_publish.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def _run(
    command: Sequence[str],
    *,
    cwd: Path = PROJECT_ROOT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "오류 내용 없음").strip()
        raise RuntimeError(f"명령 실패({result.returncode}): {' '.join(command)}\n{detail}")
    return result


def _database_last_date(path: Path, table: str) -> str | None:
    if not path.exists():
        return None
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=10,
    )
    try:
        row = connection.execute(
            f'SELECT MAX(trade_date) FROM "{table}"'
        ).fetchone()
        return str(row[0]) if row and row[0] else None
    finally:
        connection.close()


def _latest_trade_date() -> str:
    history_date = _database_last_date(
        PROJECT_ROOT / "data" / "history.db",
        "daily_market_metrics",
    )
    listed_date = _database_last_date(
        PROJECT_ROOT / "data" / "krx_listed_history.db",
        "krx_listed_daily",
    )
    if history_date is None or listed_date is None:
        raise RuntimeError("배포할 일별 시장 DB가 비어 있습니다.")
    if history_date != listed_date:
        raise RuntimeError(
            "두 배포 DB의 마지막 거래일이 다릅니다: "
            f"history={history_date}, krx_listed={listed_date}"
        )
    return history_date


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _repository_name() -> str:
    result = _run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    )
    repository = result.stdout.strip()
    if not repository or "/" not in repository:
        raise RuntimeError("GitHub 저장소 이름을 확인하지 못했습니다.")
    return repository


def _build_assets(output_root: Path, repository: str) -> dict[str, dict[str, Any]]:
    assets: dict[str, dict[str, Any]] = {}
    for key, spec in ASSET_SPECS.items():
        output_path = output_root / str(spec["name"])
        _run(
            [
                sys.executable,
                str(spec["builder"]),
                "--source",
                str(spec["source"]),
                "--output",
                str(output_path),
            ]
        )
        digest = _sha256(output_path)
        assets[key] = {
            "name": output_path.name,
            "url": (
                f"https://github.com/{repository}/releases/download/"
                f"{RELEASE_TAG}/{output_path.name}?v={digest[:16]}"
            ),
            "sha256": digest,
            "size": output_path.stat().st_size,
            "path": output_path,
        }
    return assets


def _manifest_payload(
    latest_trade_date: str,
    assets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "latest_trade_date": latest_trade_date,
        "published_at": datetime.now(KST).isoformat(timespec="seconds"),
        "release_tag": RELEASE_TAG,
        "assets": {
            key: {
                "name": str(value["name"]),
                "url": str(value["url"]),
                "sha256": str(value["sha256"]),
                "size": int(value["size"]),
            }
            for key, value in assets.items()
        },
    }


def _same_release_content(
    existing: dict[str, Any] | None,
    candidate: dict[str, Any],
) -> bool:
    if not existing:
        return False
    return (
        existing.get("schema_version") == candidate.get("schema_version")
        and existing.get("latest_trade_date") == candidate.get("latest_trade_date")
        and existing.get("release_tag") == candidate.get("release_tag")
        and existing.get("assets") == candidate.get("assets")
    )


def _remote_manifest() -> dict[str, Any] | None:
    result = _run(
        ["git", "show", "origin/main:data/daily_seed_manifest.json"],
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _ensure_release(repository: str) -> None:
    existing = _run(
        ["gh", "release", "view", RELEASE_TAG, "--repo", repository],
        check=False,
    )
    if existing.returncode == 0:
        return
    _run(
        [
            "gh",
            "release",
            "create",
            RELEASE_TAG,
            "--repo",
            repository,
            "--title",
            "NXT Dashboard daily market data",
            "--notes",
            "Streamlit 배포에서 사용하는 최신 확정 일별 시장 DB입니다.",
        ]
    )


def _upload_assets(repository: str, assets: dict[str, dict[str, Any]]) -> None:
    _ensure_release(repository)
    _run(
        [
            "gh",
            "release",
            "upload",
            RELEASE_TAG,
            *(str(value["path"]) for value in assets.values()),
            "--repo",
            repository,
            "--clobber",
        ]
    )


def _publish_manifest(
    manifest: dict[str, Any],
    temporary_root: Path,
) -> None:
    worktree = temporary_root / "deploy-worktree"
    _run(["git", "worktree", "add", "--detach", str(worktree), "origin/main"])
    try:
        target = worktree / "data" / "daily_seed_manifest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _run(["git", "add", "--", "data/daily_seed_manifest.json"], cwd=worktree)
        changed = _run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=worktree,
            check=False,
        )
        if changed.returncode == 0:
            return
        if changed.returncode != 1:
            raise RuntimeError("manifest 변경 여부를 확인하지 못했습니다.")
        _run(
            [
                "git",
                "commit",
                "-m",
                f"Update market data through {manifest['latest_trade_date']}",
            ],
            cwd=worktree,
        )
        _run(["git", "push", "origin", "HEAD:main"], cwd=worktree)
    finally:
        _run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            check=False,
        )


def publish_daily_seeds(*, force: bool = False) -> bool:
    logger = _logger()
    latest_trade_date = _latest_trade_date()
    _run(["gh", "auth", "status"])
    repository = _repository_name()
    _run(["git", "fetch", "origin", "main"])
    existing_manifest = _remote_manifest()
    if (
        not force
        and existing_manifest is not None
        and existing_manifest.get("latest_trade_date") == latest_trade_date
    ):
        logger.info("새 확정 거래일 없음: 최근 거래일=%s", latest_trade_date)
        return False
    with tempfile.TemporaryDirectory(prefix="nxt-daily-deploy-") as directory:
        temporary_root = Path(directory)
        assets = _build_assets(temporary_root, repository)
        manifest = _manifest_payload(latest_trade_date, assets)
        if not force and _same_release_content(existing_manifest, manifest):
            logger.info("배포 DB 변경 없음: 최근 거래일=%s", latest_trade_date)
            return False
        _upload_assets(repository, assets)
        _publish_manifest(manifest, temporary_root)
    logger.info(
        "배포 DB 자동 게시 완료: 최근 거래일=%s release=%s",
        latest_trade_date,
        RELEASE_TAG,
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="확정 일별 시장 DB를 GitHub Release에 게시하고 Streamlit을 재배포합니다."
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        publish_daily_seeds(force=args.force)
        return 0
    except Exception:
        _logger().exception("배포 DB 자동 게시 실패")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
