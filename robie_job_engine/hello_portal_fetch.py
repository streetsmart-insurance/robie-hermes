"""Fetch carrier-portal documents behind a login, for the hello intake match() step.

Carlo's direction: carrier notices often link to documents that live
behind a carrier portal login. The worker reads the portal logins from
GCP Secret Manager ("it can login with gcp") instead of deferring every
portal link to a human.

Pipeline: link URL -> portal registry (domain match) -> read
username/password from Secret Manager via ADC -> Playwright on the box
Chrome signs in (ONE attempt, no retry loop) -> navigate to the document
URL -> return the file bytes to hello_match's extraction pipeline.

Standing safety rules (do not relax):
- Secret VALUES are transient-only: read at runtime on hermes-poc-01
  via Application Default Credentials. Never a key file baked into the
  repo, never logged, never persisted, never in error messages. Code
  and comments record only secret NAMES.
- Unknown portal domains still defer to a human (portal_link_needs_human).
- Login failure / timeout / empty document -> defer, exactly like an
  unknown portal. The deferral is the fallback, never a silent drop.
- Timeouts on every network and browser operation.

Read-only: this module never writes to EZLynx, never sends mail, never
calls Zapier. All secrets access and all browser driving are injected
(fakes in tests); the real paths only run on the box worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Portal registry: domain -> how to sign in
# ---------------------------------------------------------------------------

# Box Chrome / Playwright conventions (hermes-poc-01).
_BOX_CHROME_PATH = "/usr/bin/google-chrome"
_BOX_PROXY = "http://9.142.10.166:5822"

# Largest document we will accept from a portal (25 MiB).
_MAX_PORTAL_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class PortalConfig:
    """How to sign in to one carrier portal.

    Only secret NAMES are recorded here — never values. The values are
    read at runtime from GCP Secret Manager (project
    streetsmart-hermes-poc) and live only in local variables.
    """

    domain: str                       # e.g. "agents.examplecarrier.com"
    username_secret: str              # full resource name:
                                      # projects/<p>/secrets/<name>/versions/latest
    password_secret: str              # full resource name
    login_url: str                    # https://... sign-in page
    handler: str = "generic_form"     # key into HANDLERS


_PORTALS: dict[str, PortalConfig] = {}


def _valid_secret_ref(ref: str) -> bool:
    return (isinstance(ref, str) and ref.startswith("projects/")
            and "/secrets/" in ref and "/versions/" in ref)


def register_portal(cfg: PortalConfig) -> None:
    """Add (or replace) a portal entry. Validates shape, not credentials."""
    if not isinstance(cfg, PortalConfig):
        raise TypeError("register_portal needs a PortalConfig")
    domain = (cfg.domain or "").strip().lower()
    if not domain:
        raise ValueError("PortalConfig.domain is required")
    if not _valid_secret_ref(cfg.username_secret):
        raise ValueError(
            "username_secret must be a full Secret Manager resource name "
            "(projects/<p>/secrets/<name>/versions/<v>)")
    if not _valid_secret_ref(cfg.password_secret):
        raise ValueError(
            "password_secret must be a full Secret Manager resource name "
            "(projects/<p>/secrets/<name>/versions/<v>)")
    if not (cfg.login_url or "").lower().startswith("https://"):
        raise ValueError("login_url must be an https URL")
    _PORTALS[domain] = PortalConfig(
        domain=domain,
        username_secret=cfg.username_secret.strip(),
        password_secret=cfg.password_secret.strip(),
        login_url=cfg.login_url.strip(),
        handler=(cfg.handler or "generic_form").strip() or "generic_form",
    )


def portal_for_url(url: str) -> PortalConfig | None:
    """Return the portal entry for a URL's domain, or None if unknown.

    Matches the exact host, else the longest registered domain that is a
    parent of the host (docs.agents.example.com matches
    agents.example.com). Unknown domains -> None -> human deferral.
    """
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:  # noqa: BLE001 - unparsable URL defers
        return None
    if not host:
        return None
    if host in _PORTALS:
        return _PORTALS[host]
    best: PortalConfig | None = None
    for domain, cfg in _PORTALS.items():
        if host.endswith("." + domain) and (
                best is None or len(domain) > len(best.domain)):
            best = cfg
    return best


# ---------------------------------------------------------------------------
# Browser driver: Playwright on the box Chrome (real path, lazily imported)
# ---------------------------------------------------------------------------

# handler(page, portal, username, password, target_url, op_timeout_ms) -> bytes
Handler = Callable[..., bytes]
HANDLERS: dict[str, Handler] = {}


def register_handler(name: str, fn: Handler) -> None:
    """Plug in a per-portal handler. 'generic_form' is the default."""
    if not name or not callable(fn):
        raise ValueError("register_handler needs a name and a callable")
    HANDLERS[name.strip()] = fn


def _first_visible(page, selectors: list[str], op_timeout_ms: int):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=min(op_timeout_ms, 8000))
            return loc
        except Exception:  # noqa: BLE001 - try the next selector
            continue
    return None


def generic_form_handler(page, portal: PortalConfig, username: str,
                         password: str, target_url: str,
                         op_timeout_ms: int) -> bytes:
    """Sign in through a standard username/password form, then fetch.

    Fills the first visible username-ish field and the password field,
    submits, waits for the login form to go away, then navigates to the
    document URL and returns its bytes (download or response body).
    """
    page.goto(portal.login_url, timeout=op_timeout_ms,
              wait_until="domcontentloaded")
    user_field = _first_visible(page, [
        "input[type='email']",
        "input[name*='user' i]",
        "input[name*='login' i]",
        "input[name*='email' i]",
        "input[type='text']",
    ], op_timeout_ms)
    pass_field = _first_visible(page, ["input[type='password']"],
                                op_timeout_ms)
    if user_field is None or pass_field is None:
        raise RuntimeError("login form fields not found")
    user_field.fill(username)
    pass_field.fill(password)
    # Local copies are transient; never logged.
    submit = _first_visible(page, [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Sign in')",
        "button:has-text('Log in')",
        "button:has-text('Login')",
    ], op_timeout_ms)
    if submit is None:
        pass_field.press("Enter")
    else:
        submit.click()
    try:
        page.wait_for_load_state("networkidle", timeout=op_timeout_ms)
    except Exception:  # noqa: BLE001 - page may keep polling; not fatal
        pass
    # Fail closed: if a password field is still visible we did not sign in.
    try:
        still_there = page.locator("input[type='password']").count() > 0
    except Exception:  # noqa: BLE001
        still_there = False
    if still_there:
        raise RuntimeError("sign-in did not complete")
    # The document URL: prefer a real download, else the response body.
    try:
        with page.expect_download(timeout=op_timeout_ms) as dl_info:
            page.goto(target_url, timeout=op_timeout_ms,
                      wait_until="domcontentloaded")
        download = dl_info.value
        path = download.path()
        with open(path, "rb") as fh:
            return fh.read(_MAX_PORTAL_BYTES + 1)
    except Exception:  # noqa: BLE001 - not a download; read the response
        pass
    resp = page.goto(target_url, timeout=op_timeout_ms,
                     wait_until="domcontentloaded")
    if resp is None or not resp.ok:
        raise RuntimeError("document URL did not return a document")
    body = resp.body()
    if not body:
        raise RuntimeError("document URL returned no bytes")
    return body


register_handler("generic_form", generic_form_handler)


def _playwright_login_fetch(portal: PortalConfig, username: str,
                            password: str, target_url: str,
                            timeout_secs: int) -> bytes:
    """Real browser path: Playwright driving the box Chrome.

    Imported lazily so unit tests (and machines without Playwright) can
    import this module. Runs ONLY on hermes-poc-01 via the worker.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("playwright is required for portal fetch") from exc
    op_timeout_ms = max(10_000, min(60_000, int(timeout_secs * 1000 // 3)))
    handler = HANDLERS.get(portal.handler)
    if handler is None:
        raise RuntimeError("no handler registered")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=_BOX_CHROME_PATH,
            args=["--proxy-server=%s" % _BOX_PROXY,
                  "--disable-blink-features=AutomationControlled"])
        try:
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            try:
                return handler(page, portal, username, password,
                               target_url, op_timeout_ms)
            finally:
                page.close()
                context.close()
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_portal_document(url: str, *, secret_accessor=None,
                          browser_fetch=None,
                          timeout_secs: int = 90) -> tuple[bytes | None, str]:
    """Attempt to fetch a portal document behind a login.

    Returns (bytes, "") on success, or (None, reason) where reason is one
    of: unknown_portal | unknown_handler | secret_unavailable |
    login_failed | empty_document | document_too_large. The reason never
    contains credential values — only the portal domain.

    secret_accessor: object with .access(resource_name) -> str. Defaults
      to GoogleSecretManagerAccessor (ADC on the box). Inject a fake in
      tests.
    browser_fetch(portal, username, password, target_url, timeout_secs)
      -> bytes. Defaults to the Playwright box-Chrome path. Inject a fake
      in tests. Exactly ONE login attempt is made — never a retry loop.
    """
    portal = portal_for_url(url or "")
    if portal is None:
        return None, "unknown_portal"
    handler = HANDLERS.get(portal.handler)
    if handler is None:
        return None, "unknown_handler"

    if secret_accessor is None:
        try:
            from .secret_manager import GoogleSecretManagerAccessor
        except ImportError as exc:
            return None, "secret_unavailable"
        try:
            secret_accessor = GoogleSecretManagerAccessor()
        except Exception:  # noqa: BLE001 - ADC missing etc.
            return None, "secret_unavailable"

    try:
        username = secret_accessor.access(portal.username_secret)
        password = secret_accessor.access(portal.password_secret)
    except Exception:  # noqa: BLE001 - do not leak why
        return None, "secret_unavailable"
    if not username or not password:
        return None, "secret_unavailable"

    fetch = browser_fetch or _playwright_login_fetch
    try:
        # Single attempt. A failed login defers to a human; it is never
        # retried in a loop (account lockout risk).
        data = fetch(portal, username, password, url, timeout_secs)
    except Exception:  # noqa: BLE001 - reason carries no secrets
        return None, "login_failed"
    if not data:
        return None, "empty_document"
    if len(data) > _MAX_PORTAL_BYTES:
        return None, "document_too_large"
    return data, ""


def make_portal_fetch(*, secret_accessor=None, browser_fetch=None,
                      timeout_secs: int = 90):
    """Build the portal_fetch callable hello_match.retrieve_link_docs takes.

    portal_fetch(url) -> (bytes | None, reason). The worker on the box
    calls make_portal_fetch() with no arguments (ADC + box Chrome);
    tests inject fakes.
    """
    def _fetch(url: str) -> tuple[bytes | None, str]:
        return fetch_portal_document(
            url, secret_accessor=secret_accessor,
            browser_fetch=browser_fetch, timeout_secs=timeout_secs)
    return _fetch
