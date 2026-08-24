"""Configuration loading for prospecting_plugin.

Fails fast: load_config() collects every problem (missing/invalid env vars)
and raises ONE ConfigError listing all of them, so a bad deploy shows the
whole punch list instead of one var at a time. Also owns a masked-logging
setup so a token or database URL can never land in plaintext in the logs.

DRY_RUN is env-pinned and defaults to TRUE: only an explicit "false"/"0"/"no"
in the environment turns it off. That is a safety invariant -- nothing in a
request body can ever flip a deployment live, because the value is decided
here, once, at boot, from the environment alone.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_APP_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _APP_ROOT / ".env"

# The ONLY strings that turn DRY_RUN off. Anything else -- empty, unset,
# "true", garbage, a typo -- leaves the deployment pinned to dry runs.
_DRY_RUN_OFF_VALUES = ("false", "0", "no")


class ConfigError(ValueError):
    """Raised once per load_config() call with every problem found, not just
    the first -- see the module docstring."""


@dataclass(frozen=True)
class AppConfig:
    database_url: str
    hubspot_token: str = ""
    hubspot_portal_id: str = ""
    fullenrich_api_key: str = ""
    extension_origin: str = ""
    dry_run: bool = True
    host: str = "127.0.0.1"
    port: int = 8080
    slack_webhook_url: str = ""


def load_config(env_path: str | Path | None = None) -> AppConfig:
    """Load .env from the app dir, then every var this app reads. Raises
    ConfigError (a ValueError) with the full list of problems if anything is
    wrong; returns a ready-to-use AppConfig otherwise.

    Phase 0: only PROSPECTOR_DATABASE_URL is hard-required. HUBSPOT_TOKEN,
    FULLENRICH_API_KEY, and EXTENSION_ORIGIN may be empty -- preflight warns
    about them instead of failing, because --check must be usable before the
    integrations exist.
    """
    load_dotenv(env_path or _ENV_PATH)
    problems: list[str] = []

    def get(name: str, default: str = "") -> str:
        return os.getenv(name, default).strip()

    def get_int(name: str, default: str) -> int:
        raw = get(name, default) or default
        try:
            return int(raw)
        except ValueError:
            problems.append(f"{name} must be an integer (got {raw!r})")
            return int(default)

    database_url = get("PROSPECTOR_DATABASE_URL")
    if not database_url:
        problems.append("PROSPECTOR_DATABASE_URL is not set")
    else:
        database_url = _ensure_sslmode(database_url)

    hubspot_token = get("HUBSPOT_TOKEN")
    hubspot_portal_id = get("HUBSPOT_PORTAL_ID")
    fullenrich_api_key = get("FULLENRICH_API_KEY")
    extension_origin = get("EXTENSION_ORIGIN")

    # Safety invariant: dry-run by default; ONLY an explicit off-value
    # (case-insensitive) goes live. A request body can never override this.
    dry_run = get("DRY_RUN").lower() not in _DRY_RUN_OFF_VALUES

    host = get("HOST", "127.0.0.1")
    port = get_int("PORT", "8080")
    if not (1 <= port <= 65535):
        problems.append(f"PORT must be between 1 and 65535 (got {port})")

    slack_webhook_url = get("SLACK_WEBHOOK_URL")

    if problems:
        raise ConfigError(
            "prospecting_plugin configuration is invalid -- fix ALL of the following:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )

    return AppConfig(
        database_url=database_url,
        hubspot_token=hubspot_token,
        hubspot_portal_id=hubspot_portal_id,
        fullenrich_api_key=fullenrich_api_key,
        extension_origin=extension_origin,
        dry_run=dry_run,
        host=host,
        port=port,
        slack_webhook_url=slack_webhook_url,
    )


def phase0_warnings(cfg: AppConfig) -> list[str]:
    """Config-level warnings for --check / boot logs. These vars may be empty
    in Phase 0, but an operator should see exactly which integrations are
    dark. Never includes the values themselves -- only the var names."""
    warnings: list[str] = []
    if not cfg.hubspot_token:
        warnings.append("HUBSPOT_TOKEN is empty -- HubSpot writes are unavailable")
    if not cfg.fullenrich_api_key:
        warnings.append("FULLENRICH_API_KEY is empty -- enrichment providers are unavailable")
    if not cfg.extension_origin:
        # NOT "rejects all browsers" -- the actual behavior is the opposite:
        # server.py treats an empty origin as disabled-OPEN and echoes any
        # Origin back. Bearer auth is the only gate in that state.
        warnings.append(
            "EXTENSION_ORIGIN is empty -- CORS is disabled-open (any origin "
            "echoed); bearer auth remains the only gate. Must be set before "
            "DRY_RUN is turned off."
        )
    return warnings


def _ensure_sslmode(db_url: str) -> str:
    """libpq defaults to sslmode=prefer, which silently allows a plaintext
    connection if TLS negotiation fails. Force sslmode=require unless the
    caller already specified a mode."""
    if "sslmode=" in db_url.lower():
        return db_url
    if "://" in db_url:
        separator = "&" if "?" in db_url else "?"
        return f"{db_url}{separator}sslmode=require"
    # libpq keyword/value DSN ("host=... user=... "): space-separated pairs.
    return f"{db_url.rstrip()} sslmode=require"


# ---------------------------------------------------------------------------
# Masked logging: tokens and the database URL must never land in plaintext in
# application logs. This is a safety net -- library modules already avoid
# logging secrets by convention -- for anything that slips through anyway
# (tracebacks, unexpected str(exc) content, etc).
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")


class MaskedLogFilter(logging.Filter):
    """Rewrites any email address in a log record's message to ***@<domain>,
    and blanks out any exact secret string passed in (tokens, the DSN)."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:
            return True

        redacted = _EMAIL_RE.sub(lambda m: f"***@{m.group(1)}", text)
        for secret in self._secrets:
            if secret and secret in redacted:
                redacted = redacted.replace(secret, "***REDACTED***")

        if redacted != text:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    """Install one StreamHandler on the root logger, formatted per project
    convention, with MaskedLogFilter attached. Safe to call more than once
    (e.g. from tests) -- it replaces any handlers already installed rather
    than stacking duplicates."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    handler.addFilter(MaskedLogFilter(secrets))

    root.handlers = [handler]
