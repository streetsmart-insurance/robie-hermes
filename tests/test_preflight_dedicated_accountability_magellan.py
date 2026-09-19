from pathlib import Path

from scripts.preflight_dedicated_accountability_magellan import (
    _is_authenticated,
    _safe_page_state,
)


class _Locator:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class _Page:
    def __init__(self, url, *, password=False, email=False, google=False, microsoft=False):
        self.url = url
        self._password = password
        self._email = email
        self._google = google
        self._microsoft = microsoft

    def locator(self, selector):
        if "password" in selector:
            return _Locator(int(self._password))
        return _Locator(int(self._email))

    def get_by_role(self, _role, *, name):
        return _Locator(int(self._google if name == "Sign in with Google" else self._microsoft))


def test_accepts_authenticated_magellan_application_route():
    assert _is_authenticated(_Page("https://app.magellan.insure/dashboard"))
    assert _is_authenticated(_Page("https://app.magellan.insure/calls"))


def test_rejects_login_and_foreign_routes():
    assert not _is_authenticated(_Page("https://app.magellan.insure/login"))
    assert not _is_authenticated(_Page("https://accounts.google.com/signin"))
    assert not _is_authenticated(
        _Page("https://app.magellan.insure/dashboard", password=True)
    )


def test_safe_state_contains_no_query_or_fragment(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    state_path.chmod(0o600)
    state = _safe_page_state(
        _Page(
            "https://app.magellan.insure/login?token=secret#fragment",
            password=True,
            email=True,
            google=True,
            microsoft=True,
        ),
        state_path,
    )
    assert state["host"] == "app.magellan.insure"
    assert state["path"] == "/login"
    assert "secret" not in str(state)
    assert state["storage_state_mode"] == "0o600"
