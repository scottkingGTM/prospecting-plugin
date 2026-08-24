"""Unit tests for prospector.config — env parsing only, no DB, no network.

The DRY_RUN cases are the ones that matter most: the default must be TRUE
and only an explicit "false"/"0"/"no" may turn it off, because that flag is
the safety invariant standing between a fresh deploy and live writes.
"""

from __future__ import annotations

import pytest

from prospector.config import ConfigError, load_config

# All the vars load_config() reads — cleared before every test so the
# developer's real shell env (or a stray .env) can never leak in.
_ALL_VARS = (
    "PROSPECTOR_DATABASE_URL",
    "HUBSPOT_TOKEN",
    "HUBSPOT_PORTAL_ID",
    "FULLENRICH_API_KEY",
    "EXTENSION_ORIGIN",
    "DRY_RUN",
    "HOST",
    "PORT",
    "SLACK_WEBHOOK_URL",
)

_DB_URL = "postgresql://prospector:pw@db.example.com:5432/postgres"


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Scrub every prospector env var and return a load() helper that points
    dotenv at a nonexistent file, so only what the test sets is visible."""
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)

    def load():
        return load_config(env_path=tmp_path / "does-not-exist.env")

    return load


def _set_db_url(monkeypatch):
    monkeypatch.setenv("PROSPECTOR_DATABASE_URL", _DB_URL)


# -- DRY_RUN: the safety invariant ------------------------------------------


def test_dry_run_defaults_to_true(clean_env, monkeypatch):
    _set_db_url(monkeypatch)
    cfg = clean_env()
    assert cfg.dry_run is True


@pytest.mark.parametrize("value", ["false", "FALSE", "False", "0", "no", "No", "NO"])
def test_dry_run_explicit_off_values(clean_env, monkeypatch, value):
    _set_db_url(monkeypatch)
    monkeypatch.setenv("DRY_RUN", value)
    cfg = clean_env()
    assert cfg.dry_run is False


@pytest.mark.parametrize("value", ["", "true", "TRUE", "1", "yes", "garbage", "off", "nope", " "])
def test_dry_run_anything_else_stays_true(clean_env, monkeypatch, value):
    _set_db_url(monkeypatch)
    monkeypatch.setenv("DRY_RUN", value)
    cfg = clean_env()
    assert cfg.dry_run is True


# -- required vars ------------------------------------------------------------


def test_missing_database_url_raises_with_var_name(clean_env):
    with pytest.raises(ValueError) as excinfo:
        clean_env()
    assert "PROSPECTOR_DATABASE_URL" in str(excinfo.value)


def test_config_error_is_a_value_error(clean_env):
    with pytest.raises(ConfigError):
        clean_env()


def test_optional_vars_may_be_empty_in_phase0(clean_env, monkeypatch):
    _set_db_url(monkeypatch)
    cfg = clean_env()
    assert cfg.hubspot_token == ""
    assert cfg.fullenrich_api_key == ""
    assert cfg.extension_origin == ""


# -- host / port ----------------------------------------------------------------


def test_port_defaults(clean_env, monkeypatch):
    _set_db_url(monkeypatch)
    cfg = clean_env()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8080


def test_port_parses_integer(clean_env, monkeypatch):
    _set_db_url(monkeypatch)
    monkeypatch.setenv("PORT", "9090")
    cfg = clean_env()
    assert cfg.port == 9090


def test_port_garbage_raises_with_var_name(clean_env, monkeypatch):
    _set_db_url(monkeypatch)
    monkeypatch.setenv("PORT", "not-a-port")
    with pytest.raises(ValueError) as excinfo:
        clean_env()
    assert "PORT" in str(excinfo.value)


def test_port_out_of_range_raises(clean_env, monkeypatch):
    _set_db_url(monkeypatch)
    monkeypatch.setenv("PORT", "70000")
    with pytest.raises(ValueError) as excinfo:
        clean_env()
    assert "PORT" in str(excinfo.value)


# -- error aggregation ------------------------------------------------------------


def test_all_problems_reported_at_once(clean_env, monkeypatch):
    monkeypatch.setenv("PORT", "junk")
    with pytest.raises(ValueError) as excinfo:
        clean_env()
    message = str(excinfo.value)
    assert "PROSPECTOR_DATABASE_URL" in message
    assert "PORT" in message


# -- sslmode hardening -------------------------------------------------------------


def test_sslmode_appended_when_missing(clean_env, monkeypatch):
    _set_db_url(monkeypatch)
    cfg = clean_env()
    assert "sslmode=require" in cfg.database_url


def test_sslmode_respected_when_present(clean_env, monkeypatch):
    monkeypatch.setenv("PROSPECTOR_DATABASE_URL", _DB_URL + "?sslmode=verify-full")
    cfg = clean_env()
    assert cfg.database_url.count("sslmode=") == 1
    assert "sslmode=verify-full" in cfg.database_url
