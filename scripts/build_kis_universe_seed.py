from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="KIS NXT 대상·대사 SQLite를 배포용 gzip 시드로 압축합니다."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_ROOT / "data" / "kis_universe.db",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data" / "kis_universe.db.gz",
    )
    args = parser.parse_args()
    if not args.database.exists():
        raise FileNotFoundError(args.database)
    with sqlite3.connect(args.database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(f"{args.output.suffix}.tmp")
    with args.database.open("rb") as source, gzip.GzipFile(
        filename=str(temporary), mode="wb", mtime=0
    ) as target:
        shutil.copyfileobj(source, target)
    temporary.replace(args.output)
    print(f"created {args.output} ({args.output.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
