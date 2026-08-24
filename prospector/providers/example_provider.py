"""Template enrichment adapter -- copy this to write your own.

This is a minimal, annotated skeleton showing the two methods every provider
must implement. It is NOT registered anywhere and never runs in production;
see fullenrich.py for a complete, battle-tested reference implementation
against a real vendor API (retries, polling, billing reconciliation).

To plug in your own enrichment vendor:

  1. Copy this file to prospector/providers/<yourvendor>.py and fill in cost()
     and resolve() against your vendor's HTTP API.
  2. Add your API key to AppConfig in prospector/config.py (read from an env
     var, exactly like fullenrich_api_key).
  3. Register the adapter: add one line to `adapter_specs` in
     prospector/providers/__init__.py:
         "<yourvendor>": (YourAdapter, cfg.yourvendor_api_key),
  4. Turn it on in data: INSERT a prospector.providers row with id
     '<yourvendor>' and add prospector.waterfalls legs that reference it.

See PROVIDERS.md for the full walkthrough.
"""

from __future__ import annotations

from collections.abc import Sequence

from . import ContactResult, EnrichInput, ProviderAdapter, ProviderError

# List price per field, in whatever "credit" unit your billing/caps use.
# cost() quotes the WORST case; the waterfall books what the call actually
# billed at settle time.
_LIST_PRICE = {
    "work_email": 1.0,
    "personal_email": 3.0,
    "mobile": 10.0,
}


class ExampleAdapter(ProviderAdapter):
    """A do-nothing template. Replace the body of resolve() with a real call.

    Constructor signature matches what build_registry() calls:
    `Adapter(api_key, config=<dict>)`.
    """

    id = "example"
    kind = "lookup"  # 'lookup' (real data source) or 'inference' (model-guessed)
    supports = frozenset(("work_email", "personal_email", "mobile"))

    def __init__(self, api_key: str, *, config: dict | None = None) -> None:
        if not api_key:
            raise ValueError("example provider API key is empty")
        self._api_key = api_key
        self._config = config or {}

    def cost(self, fields: Sequence[str]) -> float:
        """Worst-case credits to enrich one person for these fields.

        Raise ValueError for a field this adapter cannot handle -- never
        forward an unknown field to the vendor.
        """
        requested = set(fields)
        unknown = requested - self.supports
        if unknown:
            raise ValueError(f"unsupported field(s): {sorted(unknown)}")
        return sum(_LIST_PRICE[f] for f in requested)

    def resolve(self, inp: EnrichInput, fields: Sequence[str]) -> ContactResult:
        """Enrich one person and return a ContactResult.

        Contract:
          * ValueError    for a CALLER bug (unknown field, no usable identity).
          * ProviderError for anything the VENDOR or the wire did wrong. The
            waterfall will NOT retry a ProviderError -- do your own transport
            retries inside this method (see fullenrich.py), then raise.

        `inp` carries linkedin_url / first_name / last_name / company_domain /
        company_name -- send your vendor whatever identity it needs.

        Map the vendor's response into EmailHit / PhoneHit objects. For an
        email, `status` is one of 'verified' | 'risky' | 'unknown' |
        'inferred' -- and 'inferred' (a pattern-guessed address) can never be
        committed to the CRM (guards.inferred_email_guard blocks it), so mark
        guessed addresses honestly.

        Put the raw vendor payload in meta['raw_response'] for the audit
        ledger; it is stripped before anything reaches the browser.
        """
        raise ProviderError(
            "ExampleAdapter is a template -- implement resolve() against your "
            "vendor's API (see fullenrich.py for a complete example)."
        )

        # A real implementation ends by returning something like:
        #
        # return ContactResult(
        #     emails=[EmailHit(address="jane@acme.com", type="work",
        #                      status="verified", provider=self.id,
        #                      cost_credits=_LIST_PRICE["work_email"])],
        #     phones=[],
        #     profile={"first_name": "Jane", "last_name": "Doe"},
        #     company={"domain": "acme.com"},
        #     meta={"provider_id": self.id, "raw_response": <vendor json>},
        # )
