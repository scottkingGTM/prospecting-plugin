#!/usr/bin/env python3
"""prospecting_plugin — entry point.

Usage:
  python run.py --check      validate config + DB privileges, print a report, exit
  python run.py --serve      run preflight, then start the HTTP server

Every mode fails fast and LOUDLY: a misconfigured deploy exits 2 with the
whole punch list rather than starting up against the wrong role or with
over-privileged CRM access. Exit codes: 0 ok; 2 configuration/preflight
problem, and also what argparse itself exits with on a bad flag; 1 is used
only by the explicit "no mode chosen" path below.
"""

from __future__ import annotations

import argparse
import logging
import sys

from prospector.config import (
    AppConfig,
    ConfigError,
    load_config,
    phase0_warnings,
    setup_logging,
)
from prospector.database import Database

logger = logging.getLogger("prospecting_plugin")

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PROBLEM = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run.py", description="Prospecting Plugin (extension backend)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="validate config and DB privileges, print a report, then exit (no serving)",
    )
    mode.add_argument(
        "--serve",
        action="store_true",
        help="run preflight, then start the HTTP server",
    )
    args = parser.parse_args(argv)

    if not args.check and not args.serve:
        parser.print_usage(sys.stderr)
        print("run.py: choose a mode: --check or --serve", file=sys.stderr)
        return EXIT_USAGE

    # -- config ------------------------------------------------------------
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        print("\nSee .env.example for every variable this app reads.", file=sys.stderr)
        return EXIT_PROBLEM

    setup_logging(
        "INFO",
        # Exact strings the masked log filter blanks out if they ever appear
        # in a message or traceback.
        secrets=(
            cfg.database_url,
            cfg.hubspot_token,
            cfg.fullenrich_api_key,
            cfg.slack_webhook_url,
        ),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # -- database ----------------------------------------------------------
    try:
        db = Database(cfg.database_url)
    except Exception as exc:  # noqa: BLE001 - psycopg2 raises a family of these
        print(
            f"Could not connect to Supabase ({type(exc).__name__}). Check "
            "PROSPECTOR_DATABASE_URL — it must be the session pooler host on "
            "port 5432 with the prospector role.",
            file=sys.stderr,
        )
        return EXIT_PROBLEM

    try:
        fatal, warnings = _preflight(cfg, db)

        if args.check:
            _print_check_report(cfg, fatal, warnings)
            return EXIT_OK if not fatal else EXIT_PROBLEM

        # --serve
        if fatal:
            print("Preflight failed — fix ALL of the following:", file=sys.stderr)
            for problem in fatal:
                print(f"  - {problem}", file=sys.stderr)
            return EXIT_PROBLEM
        for warning in warnings:
            logger.warning("%s", warning)
        if cfg.dry_run:
            logger.warning(
                "DRY_RUN=true: this deployment is pinned to dry runs — no HubSpot "
                "or enrichment writes until DRY_RUN is explicitly set to false"
            )

        # Imported here, not at module top, so --check works on a deploy
        # where server.py does not exist yet (Phase 0).
        from prospector.server import serve

        serve(cfg, db)
        return EXIT_OK
    finally:
        db.close()


def _preflight(cfg: AppConfig, db: Database) -> tuple[list[str], list[str]]:
    """Combine DB privilege/seed checks with the Phase-0 config warnings
    (empty-but-optional integration vars). Fatals block --serve; warnings
    are reported and allowed through."""
    try:
        fatal, warnings = db.preflight()
    except Exception as exc:  # noqa: BLE001
        # The DB preflight blew up, but the phase-0 config warnings were
        # collected independently -- still surface them so the operator sees
        # the whole picture in one pass.
        return (
            [f"database preflight could not run ({type(exc).__name__}: {exc})"],
            phase0_warnings(cfg),
        )
    return fatal, phase0_warnings(cfg) + warnings


def _print_check_report(cfg: AppConfig, fatal: list[str], warnings: list[str]) -> None:
    if cfg.dry_run:
        dry_run_line = "ON — no HubSpot or enrichment writes (env-pinned default)"
    else:
        dry_run_line = "OFF — LIVE: this deployment WILL write"
    print("prospecting_plugin — check report")
    print(f"  DRY_RUN         {dry_run_line}")
    print(f"  Serve on        {cfg.host}:{cfg.port}")
    print()
    for problem in fatal:
        print(f"  FATAL  {problem}")
    for warning in warnings:
        print(f"  WARN   {warning}")
    if not fatal and not warnings:
        print("  OK     all privilege and seed checks passed")
    elif not fatal:
        print("  OK     no fatal problems (warnings above are non-blocking)")
    print()
    if fatal:
        print("Result: NOT deployable — fix the FATAL lines above.")
    else:
        print("Result: deployable.")


if __name__ == "__main__":
    sys.exit(main())
