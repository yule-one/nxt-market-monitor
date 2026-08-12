from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.auth import AuthError, AuthStore


def _load_database_url(secrets_path: Path) -> str:
    environment_value = os.getenv("AUTH_DATABASE_URL", "").strip()
    if environment_value:
        return environment_value
    if not secrets_path.exists():
        return ""
    secrets = tomllib.loads(secrets_path.read_text(encoding="utf-8"))
    return str(secrets.get("AUTH_DATABASE_URL", "")).strip()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="로컬 auth.db 계정과 감사기록을 빈 PostgreSQL 인증 DB로 이전합니다."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=PROJECT_ROOT / "data" / "auth.db",
        help="이전할 로컬 SQLite 인증 DB",
    )
    parser.add_argument(
        "--secrets",
        type=Path,
        default=PROJECT_ROOT / ".streamlit" / "secrets.toml",
        help="AUTH_DATABASE_URL이 저장된 secrets.toml",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="PostgreSQL 연결과 대상 계정 존재 여부만 확인합니다.",
    )
    args = parser.parse_args()

    database_url = _load_database_url(args.secrets.resolve())
    if not database_url:
        parser.error(
            "AUTH_DATABASE_URL을 환경변수 또는 .streamlit/secrets.toml에 설정하세요."
        )
    try:
        destination = AuthStore(database_url=database_url)
        if args.check_only:
            state = "계정 있음" if destination.has_users() else "비어 있음"
            print(f"PostgreSQL 연결 성공 · 대상 상태: {state}")
            return
        result = destination.import_legacy_sqlite(args.source.resolve())
    except AuthError as exc:
        parser.error(str(exc))
    print(
        "PostgreSQL 이전 완료 · "
        f"계정 {result.users:,}개 · 기존 관리기록 {result.audit_events:,}건"
    )


if __name__ == "__main__":
    main()
