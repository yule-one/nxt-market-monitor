from __future__ import annotations

import app


class _CurrentAuthStoreStub:
    def authenticate_with_status(self) -> None:
        return None

    def request_signup(self) -> None:
        return None

    def review_signup_request(self) -> None:
        return None

    def delete_rejected_signup(self) -> None:
        return None

    def create_persistent_session(self) -> None:
        return None

    def authenticate_persistent_session(self) -> None:
        return None

    def revoke_persistent_session(self) -> None:
        return None


class _AuthStoreFactoryStub:
    def __init__(self) -> None:
        self.calls = 0
        self.clear_calls = 0
        self.current_store = _CurrentAuthStoreStub()

    def __call__(self, database_url: str, runtime_version: int):
        _ = database_url, runtime_version
        self.calls += 1
        if self.calls == 1:
            return object()  # 이전 배포에서 남은 AuthStore 객체를 재현합니다.
        return self.current_store

    def clear(self) -> None:
        self.clear_calls += 1


def test_stale_auth_resource_is_cleared_and_recreated(monkeypatch) -> None:
    factory = _AuthStoreFactoryStub()
    monkeypatch.setattr(app, "_secret_value", lambda name: "")
    monkeypatch.setattr(app, "_get_auth_store", factory)

    store = app.get_auth_store()

    assert store is factory.current_store
    assert factory.calls == 2
    assert factory.clear_calls == 1
