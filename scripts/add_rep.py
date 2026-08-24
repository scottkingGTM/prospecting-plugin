"""Generate a rep token + the INSERT statement for prospector.reps.

Usage:
    .venv/bin/python scripts/add_rep.py "Alice" alice@example.com 123456789

Prints TWO things:
  1. The bearer token — hand this to the rep (they paste it into the
     extension's options page). It is shown ONCE and stored nowhere.
  2. The INSERT SQL containing only the sha256 hash — paste this into the
     Supabase SQL editor.

Nothing is written to disk and nothing touches the network. Keep the token
out of email/Slack where possible; a password manager share is ideal.
"""

from __future__ import annotations

import hashlib
import secrets
import sys


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 1
    display_name, email, hubspot_owner_id = sys.argv[1], sys.argv[2], sys.argv[3]
    if "@" not in email:
        print(f"'{email}' does not look like an email address")
        return 1
    if not hubspot_owner_id.isdigit():
        print(f"'{hubspot_owner_id}' does not look like a HubSpot owner id (digits only)")
        return 1

    token = secrets.token_urlsafe(32)          # 43 chars, > the 32-char floor
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    print("=" * 72)
    print(f"TOKEN for {display_name} — hand to the rep, shown once, never stored:")
    print(f"\n    {token}\n")
    print("=" * 72)
    print("SQL — paste into the Supabase SQL editor (safe to re-run):\n")
    print("INSERT INTO prospector.reps")
    print("    (email, display_name, hubspot_owner_id, token_hash)")
    print("VALUES")
    print(f"    ('{email}', '{display_name}', '{hubspot_owner_id}',")
    print(f"     '{token_hash}')")
    print("ON CONFLICT (email) DO UPDATE SET")
    print("    token_hash = EXCLUDED.token_hash,")
    print("    display_name = EXCLUDED.display_name,")
    print("    hubspot_owner_id = EXCLUDED.hubspot_owner_id,")
    print("    active = true;")
    print()
    print("(Re-running with a new token ROTATES the rep's credential.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
