"""Provider abstraction for enrichment.

A provider adapter wraps ONE enrichment vendor behind a uniform interface:
quote a worst-case cost, resolve a person to a ContactResult. The waterfall
layer walks prospector.waterfalls positions and calls adapters through this
interface only -- it never imports a vendor module directly.

TO PLUG IN YOUR OWN ENRICHMENT API:
  1. Write an adapter: subclass ProviderAdapter (see fullenrich.py for a
     complete reference implementation, or example_provider.py for a minimal
     annotated template). Implement cost() and resolve().
  2. Register it: add one line to `adapter_specs` in build_registry() below,
     mapping your provider id to (YourAdapterClass, api_key_from_cfg).
  3. Turn it on in data: INSERT a row in prospector.providers with that id,
     and add prospector.waterfalls leg(s) that reference it. Enabling or
     disabling a vendor is then an UPDATE, never a deploy.
See PROVIDERS.md for the full walkthrough.

Error contract:
  * ValueError    -- the CALLER passed something an adapter must never see
                     (unknown field name, a Sales Nav URL, no identity at
                     all). A caller bug; fix the call site.
  * ProviderError -- the VENDOR or the transport failed (HTTP errors after
                     retries, a job that ended failed/canceled, a poll that
                     timed out). NOT retryable at the waterfall level: the
                     adapter already did its own transport retries, and
                     re-running a whole enrichment job risks buying the same
                     answer twice. The waterfall records the attempt and
                     moves on to the next leg (or reports the failure).
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence

from .types import (  # noqa: F401  (re-exported: this package's public surface)
    FIELDS,
    ContactResult,
    EmailHit,
    EnrichInput,
    PhoneHit,
)

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Transport/API failure inside one provider. See the module docstring:
    the waterfall must NOT retry this -- the adapter already retried what
    was safe to retry."""


class ProviderAdapter(ABC):
    """One enrichment vendor behind the uniform interface.

    Class attributes (set by each concrete adapter):
      id       -- matches prospector.providers.id exactly.
      kind     -- 'lookup' (queries a real data source) or 'inference'
                  (model-generated; held to stricter rules -- an inference
                  provider must never be allowed to write an email address as
                  someone's identity; see guards.inferred_email_guard).
      supports -- subset of FIELDS this vendor can resolve.
    """

    id: str
    kind: str
    supports: frozenset[str]

    @abstractmethod
    def cost(self, fields: Sequence[str]) -> float:
        """WORST-CASE credits for enriching one profile for these fields.

        Worst case on purpose: the budget layer reserves this amount before
        the call, and vendors that bill less than list price on a miss
        (FullEnrich work email bills only on hit) refund the difference at
        settle time -- never here."""

    @abstractmethod
    def resolve(self, inp: EnrichInput, fields: Sequence[str]) -> ContactResult:
        """Enrich one person. Raises ValueError for caller bugs and
        ProviderError for vendor/transport failures (see module docstring)."""


def build_registry(db, cfg) -> dict:
    """Construct adapters for every ENABLED row in prospector.providers.

    The table is the on/off switch (disabling a vendor is an UPDATE, not a
    deploy); this function is the only place a provider id becomes a Python
    object.

    TO ADD A VENDOR, add one entry to `adapter_specs` below:
    `"<provider_id>": (YourAdapterClass, cfg.your_api_key)`. Then enable it in
    prospector.providers and give it waterfall legs. Rules:

      * An enabled provider id with no entry in adapter_specs logs a warning
        and is SKIPPED -- an enabled row without code must not crash boot, but
        the operator should see the mismatch.
      * A registered provider whose API key is blank is EXCLUDED, with a
        warning: a keyless adapter could never succeed, and an empty registry
        is what makes the /enrich route return 503 instead of failing
        per-request.
      * providers.config is non-secret knobs only (per the table's column
        comment) -- the API key comes from cfg (the env), never from the DB.
    """
    # Imported here, not at module top: fullenrich.py imports ProviderAdapter
    # from this package, so a top-level import would be circular.
    from .fullenrich import FullEnrichAdapter

    # provider id -> (AdapterClass, api_key). This is the extension point:
    # one line per enrichment vendor you support.
    adapter_specs: dict[str, tuple[type, str]] = {
        "fullenrich": (FullEnrichAdapter, cfg.fullenrich_api_key),
    }

    registry: dict[str, ProviderAdapter] = {}
    rows = db.query(
        "SELECT id, kind, enabled, config FROM prospector.providers WHERE enabled;"
    )
    for row in rows:
        provider_id = str(row.get("id") or "")
        spec = adapter_specs.get(provider_id)
        if spec is None:
            logger.warning(
                "provider %r is enabled in prospector.providers but no adapter "
                "is registered for it in build_registry() -- skipped",
                provider_id,
            )
            continue
        adapter_cls, api_key = spec
        if not api_key:
            logger.warning(
                "provider %r is enabled but its API key is empty -- excluded "
                "from the registry (enrichment routes will report 503)",
                provider_id,
            )
            continue
        config = row.get("config") or {}
        if isinstance(config, str):  # psycopg2 may hand jsonb back as text
            try:
                config = json.loads(config)
            except ValueError:
                config = {}
        registry[provider_id] = adapter_cls(api_key, config=config)
    return registry
