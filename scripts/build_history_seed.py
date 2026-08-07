from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="history.db의 일관된 압축 배포 시드를 생성합니다."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=PROJECT_ROOT / "data" / "history.db",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data" / "history.db.gz",
    )
    args = parser.parse_args()
    source_path = args.source.resolve()
    output_path = args.output.resolve()
    if not source_path.exists():
        parser.error(f"원본 DB가 없습니다: {source_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        dir=output_path.parent,
        prefix=".history-seed-",
    ) as temporary_directory:
        temporary_root = Path(temporary_directory)
        snapshot_path = temporary_root / "history.db"
        compressed_path = temporary_root / "history.db.gz"
        source = sqlite3.connect(source_path, timeout=30)
        target = sqlite3.connect(snapshot_path, timeout=30)
        try:
            source.backup(target)
            target.execute("VACUUM")
            integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"DB 무결성 검사 실패: {integrity}")
        finally:
            target.close()
            source.close()
        with snapshot_path.open("rb") as source_file, compressed_path.open(
            "wb"
        ) as output_file:
            with gzip.GzipFile(
                filename="history.db",
                mode="wb",
                fileobj=output_file,
                mtime=0,
            ) as compressed_file:
                shutil.copyfileobj(source_file, compressed_file)
        os.replace(compressed_path, output_path)

    print(
        f"history seed 생성 완료: {output_path} "
        f"({output_path.stat().st_size:,} bytes)"
    )


if __name__ == "__main__":
    main()
