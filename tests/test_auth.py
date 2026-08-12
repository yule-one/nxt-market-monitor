from __future__ import annotations

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


def test_initial_admin_can_only_be_created_once(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    admin = make_admin(store)

    assert admin.is_admin
    assert admin.is_active
    assert store.authenticate("ADMIN", STRONG_PASSWORD) is not None
    with pytest.raises(AuthError, match="이미 설정"):
        make_admin(store)


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
