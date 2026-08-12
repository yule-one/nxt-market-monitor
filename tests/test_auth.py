from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.auth import (
    MAX_FAILED_ATTEMPTS,
    AuthConfigurationError,
    AuthError,
    AuthStore,
    PasswordPolicyError,
    hash_password,
    verify_password,
)


STRONG_PASSWORD = "Temporary123!"
NEW_PASSWORD = "ChangedPassword456!"


def make_store(tmp_path: Path) -> AuthStore:
    return AuthStore(tmp_path / "auth.db")


def make_admin(store: AuthStore):
    return store.create_initial_admin(
        username="admin",
        display_name="관리자",
        password=STRONG_PASSWORD,
    )


def test_password_hash_is_salted_and_verifiable() -> None:
    first = hash_password(STRONG_PASSWORD)
    second = hash_password(STRONG_PASSWORD)

    assert first != second
    assert verify_password(STRONG_PASSWORD, first)
    assert not verify_password("WrongPassword123!", first)


@pytest.mark.parametrize(
    "password",
    ["short1A", "onlyletterslong", "1234567890123"],
)
def test_password_policy_rejects_weak_passwords(password: str) -> None:
    with pytest.raises(PasswordPolicyError):
        hash_password(password)


@pytest.mark.parametrize(
    "employee_number",
    ["12345", "1234567", "12A456", "ABCDEF", "１２３４５６"],
)
def test_signup_requires_six_digit_employee_number(
    tmp_path: Path,
    employee_number: str,
) -> None:
    store = make_store(tmp_path)
    make_admin(store)

    with pytest.raises(AuthError, match="숫자 6자리"):
        store.request_signup(
            username="invalid-employee",
            employee_number=employee_number,
            display_name="잘못된 사번",
            password=STRONG_PASSWORD,
        )


def test_initial_admin_can_only_be_created_once(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    admin = make_admin(store)

    assert admin.is_admin
    assert admin.is_active
    assert store.authenticate("ADMIN", STRONG_PASSWORD) is not None
    with pytest.raises(AuthError, match="이미 설정"):
        make_admin(store)


def test_persistent_session_restores_user_and_stores_only_token_hash(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    admin = make_admin(store)

    token, expires_at = store.create_persistent_session(user_id=admin.id)
    restored = store.authenticate_persistent_session(token)

    assert restored is not None and restored.id == admin.id
    assert expires_at > store._now()
    assert store.path is not None
    with sqlite3.connect(store.path) as connection:
        stored_token_hash = connection.execute(
            "SELECT token_hash FROM auth_sessions"
        ).fetchone()[0]
    assert stored_token_hash != token
    assert len(stored_token_hash) == 64

    store.revoke_persistent_session(token)
    assert store.authenticate_persistent_session(token) is None


def test_deactivating_account_revokes_persistent_sessions(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    admin = make_admin(store)
    user = store.create_user(
        actor_user_id=admin.id,
        username="session-user",
        display_name="세션 사용자",
        temporary_password=STRONG_PASSWORD,
    )
    token, _ = store.create_persistent_session(user_id=user.id)

    store.set_active(
        actor_user_id=admin.id,
        target_user_id=user.id,
        is_active=False,
    )

    assert store.authenticate_persistent_session(token) is None


def test_admin_issues_account_and_user_must_change_password(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    admin = make_admin(store)
    user = store.create_user(
        actor_user_id=admin.id,
        username="operator01",
        display_name="운영자",
        temporary_password=STRONG_PASSWORD,
    )

    authenticated = store.authenticate(user.username, STRONG_PASSWORD)
    assert authenticated is not None
    assert authenticated.must_change_password

    store.change_password(
        user_id=user.id,
        current_password=STRONG_PASSWORD,
        new_password=NEW_PASSWORD,
    )

    changed = store.authenticate(user.username, NEW_PASSWORD)
    assert changed is not None
    assert not changed.must_change_password
    assert store.authenticate(user.username, STRONG_PASSWORD) is None


def test_signup_requires_admin_approval_before_login(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    admin = make_admin(store)
    applicant = store.request_signup(
        username="applicant01",
        employee_number="100123",
        display_name="신청자",
        password=STRONG_PASSWORD,
    )

    assert applicant.approval_status == "pending"
    assert applicant.employee_number == "100123"
    assert not applicant.is_active
    pending_login = store.authenticate_with_status(
        applicant.username,
        STRONG_PASSWORD,
    )
    assert pending_login.user is None
    assert pending_login.status == "pending"
    assert [user.id for user in store.list_signup_requests(actor_user_id=admin.id)] == [
        applicant.id
    ]

    approved = store.review_signup_request(
        actor_user_id=admin.id,
        target_user_id=applicant.id,
        approve=True,
    )

    assert approved.approval_status == "approved"
    assert approved.is_active
    assert approved.decision_by == admin.username
    authenticated = store.authenticate(applicant.username, STRONG_PASSWORD)
    assert authenticated is not None
    assert not authenticated.must_change_password


def test_rejected_signup_cannot_login_and_keeps_reason(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    admin = make_admin(store)
    applicant = store.request_signup(
        username="applicant02",
        employee_number="100124",
        display_name="반려 대상",
        password=STRONG_PASSWORD,
    )

    rejected = store.review_signup_request(
        actor_user_id=admin.id,
        target_user_id=applicant.id,
        approve=False,
        rejection_reason="사번을 확인해 주세요.",
    )
    result = store.authenticate_with_status(applicant.username, STRONG_PASSWORD)

    assert rejected.approval_status == "rejected"
    assert not rejected.is_active
    assert result.user is None
    assert result.status == "rejected"
    assert result.rejection_reason == "사번을 확인해 주세요."


def test_admin_can_delete_only_rejected_signup_and_reuse_identifiers(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    admin = make_admin(store)
    applicant = store.request_signup(
        username="applicant-delete",
        employee_number="100999",
        display_name="삭제 대상",
        password=STRONG_PASSWORD,
    )

    with pytest.raises(AuthError, match="반려된"):
        store.delete_rejected_signup(
            actor_user_id=admin.id,
            target_user_id=applicant.id,
        )

    store.review_signup_request(
        actor_user_id=admin.id,
        target_user_id=applicant.id,
        approve=False,
        rejection_reason="재신청 필요",
    )
    store.delete_rejected_signup(
        actor_user_id=admin.id,
        target_user_id=applicant.id,
    )

    assert store.get_user(applicant.id) is None
    recreated = store.request_signup(
        username="applicant-delete",
        employee_number="100999",
        display_name="재신청자",
        password=NEW_PASSWORD,
    )
    assert recreated.approval_status == "pending"
    actions = [
        event.action
        for event in store.list_audit_events(actor_user_id=admin.id, limit=20)
    ]
    assert "REJECTED_SIGNUP_DELETED" in actions


def test_non_admin_cannot_delete_rejected_signup(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    admin = make_admin(store)
    user = store.create_user(
        actor_user_id=admin.id,
        username="ordinary-user",
        display_name="일반 사용자",
        temporary_password=STRONG_PASSWORD,
    )
    applicant = store.request_signup(
        username="rejected-user",
        employee_number="100888",
        display_name="반려 사용자",
        password=STRONG_PASSWORD,
    )
    store.review_signup_request(
        actor_user_id=admin.id,
        target_user_id=applicant.id,
        approve=False,
    )

    with pytest.raises(AuthError, match="관리자 권한"):
        store.delete_rejected_signup(
            actor_user_id=user.id,
            target_user_id=applicant.id,
        )


def test_signup_rejects_duplicate_username_and_employee_number(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    make_admin(store)
    store.request_signup(
        username="applicant03",
        employee_number="100125",
        display_name="첫 신청자",
        password=STRONG_PASSWORD,
    )

    with pytest.raises(AuthError, match="아이디"):
        store.request_signup(
            username="applicant03",
            employee_number="100126",
            display_name="중복 아이디",
            password=STRONG_PASSWORD,
        )
    with pytest.raises(AuthError, match="사번"):
        store.request_signup(
            username="applicant04",
            employee_number="100125",
            display_name="중복 사번",
            password=STRONG_PASSWORD,
        )


def test_failed_logins_lock_account_until_admin_unlocks_it(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    admin = make_admin(store)
    user = store.create_user(
        actor_user_id=admin.id,
        username="operator02",
        display_name="운영자2",
        temporary_password=STRONG_PASSWORD,
    )

    for _ in range(MAX_FAILED_ATTEMPTS):
        assert store.authenticate(user.username, "WrongPassword123!") is None

    locked = store.get_user(user.id)
    assert locked is not None
    assert locked.locked_until is not None
    assert store.authenticate(user.username, STRONG_PASSWORD) is None

    store.unlock_user(actor_user_id=admin.id, target_user_id=user.id)
    assert store.authenticate(user.username, STRONG_PASSWORD) is not None


def test_last_active_admin_is_protected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    admin = make_admin(store)

    with pytest.raises(AuthError, match="현재 로그인한 관리자"):
        store.set_active(
            actor_user_id=admin.id,
            target_user_id=admin.id,
            is_active=False,
        )
    with pytest.raises(AuthError, match="마지막 활성 관리자"):
        store.set_role(
            actor_user_id=admin.id,
            target_user_id=admin.id,
            role="user",
        )


def test_non_admin_cannot_manage_accounts(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    admin = make_admin(store)
    user = store.create_user(
        actor_user_id=admin.id,
        username="operator03",
        display_name="운영자3",
        temporary_password=STRONG_PASSWORD,
    )

    with pytest.raises(AuthError, match="관리자 권한"):
        store.create_user(
            actor_user_id=user.id,
            username="operator04",
            display_name="운영자4",
            temporary_password=STRONG_PASSWORD,
        )


def test_deactivated_account_cannot_login_and_actions_are_audited(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    admin = make_admin(store)
    user = store.create_user(
        actor_user_id=admin.id,
        username="operator05",
        display_name="운영자5",
        temporary_password=STRONG_PASSWORD,
    )

    store.set_active(
        actor_user_id=admin.id,
        target_user_id=user.id,
        is_active=False,
    )

    assert store.authenticate(user.username, STRONG_PASSWORD) is None
    actions = [
        event.action
        for event in store.list_audit_events(actor_user_id=admin.id, limit=20)
    ]
    assert "USER_CREATED" in actions
    assert "USER_DEACTIVATED" in actions


def test_auth_store_rejects_non_postgresql_database_url() -> None:
    with pytest.raises(AuthConfigurationError, match="postgresql://"):
        AuthStore(database_url="sqlite:///auth.db")


def test_existing_auth_database_is_upgraded_as_approved(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-auth.db"
    password_hash = hash_password(STRONG_PASSWORD)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE app_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                must_change_password INTEGER NOT NULL DEFAULT 1,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO app_users (
                username, display_name, password_hash, role, is_active,
                must_change_password, created_at, updated_at
            ) VALUES ('admin', '관리자', ?, 'admin', 1, 0, ?, ?)
            """,
            (password_hash, "2026-08-12T00:00:00+00:00", "2026-08-12T00:00:00+00:00"),
        )

    store = AuthStore(database_path)
    user = store.authenticate("admin", STRONG_PASSWORD)

    assert user is not None
    assert user.approval_status == "approved"
    assert user.employee_number is None


def test_sqlite_accounts_and_audit_log_can_be_migrated(tmp_path: Path) -> None:
    source = make_store(tmp_path / "source")
    admin = make_admin(source)
    user = source.create_user(
        actor_user_id=admin.id,
        username="migrated01",
        display_name="이전 사용자",
        temporary_password=STRONG_PASSWORD,
    )
    assert source.authenticate(user.username, STRONG_PASSWORD) is not None
    source_audit_count = len(
        source.list_audit_events(actor_user_id=admin.id, limit=100)
    )

    destination = make_store(tmp_path / "destination")
    result = destination.import_legacy_sqlite(source.path)

    assert result.users == 2
    assert result.audit_events == source_audit_count
    imported_admin = destination.authenticate("admin", STRONG_PASSWORD)
    imported_user = destination.authenticate(user.username, STRONG_PASSWORD)
    assert imported_admin is not None and imported_admin.is_admin
    assert imported_user is not None and imported_user.must_change_password
    migrated_actions = [
        event.action
        for event in destination.list_audit_events(
            actor_user_id=imported_admin.id,
            limit=100,
        )
    ]
    assert "SQLITE_MIGRATION_COMPLETED" in migrated_actions

    with pytest.raises(AuthError, match="이미 계정"):
        destination.import_legacy_sqlite(source.path)
