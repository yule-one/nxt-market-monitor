from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import app


class _CookieManagerStub:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.set_calls: list[tuple[str, str, dict[str, object]]] = []
        self.delete_calls: list[tuple[str, str]] = []

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str, **kwargs: object) -> None:
        self.values[name] = value
        self.set_calls.append((name, value, kwargs))

    def delete(self, name: str, *, key: str) -> None:
        self.values.pop(name, None)
        self.delete_calls.append((name, key))


def test_browser_auth_token_reads_cookie_manager_after_refresh(monkeypatch) -> None:
    monkeypatch.setattr(app.st, "session_state", {})
    manager = _CookieManagerStub({app.AUTH_SESSION_COOKIE_NAME: "browser-token"})

    assert app._browser_auth_token(manager) == "browser-token"


def test_browser_auth_token_prefers_current_streamlit_session(monkeypatch) -> None:
    monkeypatch.setattr(
        app.st,
        "session_state",
        {app.AUTH_SESSION_TOKEN_KEY: "streamlit-session-token"},
    )
    manager = _CookieManagerStub({app.AUTH_SESSION_COOKIE_NAME: "browser-token"})

    assert app._browser_auth_token(manager) == "streamlit-session-token"


def test_cookie_set_action_uses_browser_cookie_component(monkeypatch) -> None:
    monkeypatch.setattr(
        app.st,
        "session_state",
        {
            app.AUTH_COOKIE_ACTION_KEY: {
                "operation": "set",
                "token": "new-token",
                "max_age": 3600,
            }
        },
    )
    monkeypatch.setattr(app, "_auth_cookie_is_secure", lambda: True)
    manager = _CookieManagerStub()

    app._render_auth_cookie_action(manager)  # type: ignore[arg-type]

    assert len(manager.set_calls) == 1
    name, value, options = manager.set_calls[0]
    assert name == app.AUTH_SESSION_COOKIE_NAME
    assert value == "new-token"
    assert options["max_age"] == 3600
    assert options["secure"] is True
    assert options["same_site"] == "strict"
    assert isinstance(options["expires_at"], datetime)
    assert app.AUTH_COOKIE_ACTION_KEY not in app.st.session_state


def test_cookie_delete_action_uses_browser_cookie_component(monkeypatch) -> None:
    monkeypatch.setattr(
        app.st,
        "session_state",
        {app.AUTH_COOKIE_ACTION_KEY: {"operation": "delete"}},
    )
    manager = _CookieManagerStub({app.AUTH_SESSION_COOKIE_NAME: "old-token"})

    app._render_auth_cookie_action(manager)  # type: ignore[arg-type]

    assert manager.delete_calls == [
        (app.AUTH_SESSION_COOKIE_NAME, "nxt_auth_cookie_delete")
    ]


def test_current_auth_user_restores_from_browser_cookie(monkeypatch) -> None:
    monkeypatch.setattr(app.st, "session_state", {})
    restored_user = SimpleNamespace(id=17)
    store = SimpleNamespace(
        authenticate_persistent_session=lambda token: (
            restored_user if token == "valid-token" else None
        )
    )
    monkeypatch.setattr(app, "get_auth_store", lambda: store)
    manager = _CookieManagerStub({app.AUTH_SESSION_COOKIE_NAME: "valid-token"})

    assert app._current_auth_user(manager) is restored_user  # type: ignore[arg-type]
    assert app.st.session_state[app.AUTH_SESSION_USER_ID_KEY] == 17
    assert app.st.session_state[app.AUTH_SESSION_TOKEN_KEY] == "valid-token"
