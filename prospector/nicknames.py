# Adapted from a shared people-name helper — people-name machinery only
# (credential stripping, name normalization, nickname agreement); keep
# behavior identical.
"""People-name identity helpers. Pure functions only — no I/O.

Everything that decides "do these two name strings plausibly describe the same
human?" lives here so it can be tested exhaustively without touching the
network.
"""

from __future__ import annotations

import re
import unicodedata

# Suffixes and credentials people staple onto their LinkedIn name field. The
# CRM stores these inconsistently ("Alex Mortensen" vs "Alex Mortensen, CPA"),
# so they are stripped before names are compared.
_NAME_NOISE = {
    "jr", "sr", "ii", "iii", "iv", "v",
    "cpa", "mba", "phd", "md", "jd", "cfa", "pmp", "cptd", "shrm", "sphr",
    "cscp", "cpim", "esq", "ma", "ms", "bs", "ba", "rn", "pe",
    # Trade and facilities credentials seen on real records: a BluSky VP filed
    # as "Colby Wrathall, FMP" failed to match the CRM's "Colby Wrathall" and
    # would have been created a second time.
    "fmp", "ace", "cfm", "cem", "cpp", "cip", "csm", "cpsm", "cscmp",
    "cma", "cissp", "pcc", "acc", "shrmcp", "shrmscp", "phr",
}

# A credential written after a comma ("Colby Wrathall, FMP"). Matched on the
# ORIGINAL casing: a real surname in "Last, First" order is not all-caps, so
# requiring upper case avoids eating the given name off a reversed record.
_TRAILING_CREDENTIAL_RE = re.compile(r"^[A-Z][A-Z.\s]{0,9}$")

# Common given-name diminutives. A prefix rule alone does not cover these --
# "dave" is not a prefix of "david", nor "mike" of "michael" -- and the CRM and
# the vendor routinely disagree on which form a person uses. Stored as
# frozensets of interchangeable forms; membership in a shared set means agree.
_NICKNAME_GROUPS = [
    {"mike", "michael", "mick"},
    {"dave", "david"},
    {"bob", "rob", "robert", "bobby", "robbie"},
    {"bill", "will", "william", "billy", "willie"},
    {"jim", "james", "jimmy", "jamie"},
    {"tom", "thomas", "tommy"},
    {"steve", "stephen", "steven"},
    {"rick", "rich", "richard", "dick", "ricky", "richie"},
    {"joe", "joseph", "joey"},
    {"tony", "anthony"},
    {"nick", "nicholas", "nicolas"},
    {"andy", "andrew", "drew"},
    {"ed", "edward", "eddie", "ted", "teddy"},
    {"pat", "patrick", "patricia", "patty"},
    {"larry", "lawrence"},
    {"charlie", "charles", "chuck", "charley"},
    {"jack", "john", "johnny", "jon", "jonathan"},
    {"frank", "francis", "franklin"},
    {"hank", "henry"},
    {"liz", "elizabeth", "beth", "betsy", "lisa", "eliza"},
    {"peggy", "margaret", "meg", "maggie"},
    {"sue", "susan", "susie", "suzanne"},
    {"cathy", "catherine", "kathy", "katherine", "kate", "katie"},
    {"jenny", "jennifer", "jen"},
    {"becky", "rebecca"},
    {"debbie", "deborah", "deb"},
    {"cindy", "cynthia"},
    {"sandy", "sandra"},
    {"vicky", "victoria"},
    {"terry", "terrence", "terrance"},
    {"greg", "gregory"},
    {"chris", "christopher", "christine", "christina"},
    {"dan", "daniel", "danny"},
    {"matt", "matthew"},
    {"ben", "benjamin"},
    {"sam", "samuel", "samantha"},
    {"alex", "alexander", "alexandra"},
    {"tim", "timothy"},
    {"ron", "ronald", "ronnie"},
    {"don", "donald", "donnie"},
    {"jeff", "jeffrey", "geoff", "geoffrey"},
    {"zach", "zachary", "zack"},
]
_NICKNAMES: dict[str, frozenset[str]] = {}
for _group in _NICKNAME_GROUPS:
    _frozen = frozenset(_group)
    for _form in _group:
        _NICKNAMES[_form] = _frozen


def strip_trailing_credentials(name: str | None) -> str:
    """Drop comma-separated credential suffixes: "Colby Wrathall, FMP".

    Only ALL-CAPS trailing segments are removed, so a record stored the other
    way round ("Wrathall, Colby") keeps both of its parts.
    """
    if not name or "," not in name:
        return (name or "").strip()
    head, *tail = [part.strip() for part in name.split(",")]
    kept = [part for part in tail if not _TRAILING_CREDENTIAL_RE.match(part)]
    return ", ".join([head, *kept]) if kept else head


def normalize_name(name: str | None) -> str:
    """Casefold, strip accents/punctuation, and drop credential suffixes."""
    if not name:
        return ""
    name = strip_trailing_credentials(name)
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    tokens = re.findall(r"[a-zA-Z]+", ascii_name.lower())
    kept = [t for t in tokens if t not in _NAME_NOISE]
    return " ".join(kept or tokens)


def names_agree(a: str | None, b: str | None) -> bool:
    """True when two name strings plausibly describe the same person.

    Deliberately strict about the surname and lenient about the given name:
    "Mike McHugh" matches "Michael McHugh" only via the shared surname plus a
    first-initial match, while "Michael Sorensen" vs "Merri Sorensen" -- a real
    mismatch found in the live CRM, where a contact carried someone else's
    profile URL -- is correctly rejected because M-i-c-h-a-e-l and M-e-r-r-i
    share only their initial and neither is a prefix of the other.
    """
    left, right = normalize_name(a), normalize_name(b)
    if not left or not right:
        return False
    if left == right:
        return True

    lt, rt = left.split(), right.split()
    if not lt or not rt:
        return False

    # Surnames must match outright.
    if lt[-1] != rt[-1]:
        return False

    lf, rf = lt[0], rt[0]
    if lf == rf:
        return True
    # Known diminutive pairs (Mike/Michael, Dave/David, Jack/John).
    if rf in _NICKNAMES.get(lf, frozenset()):
        return True
    # Shortenings the table doesn't list: one given name is a prefix of the
    # other (Chris/Christopher, Greg/Gregory). Requires 3+ characters so single
    # initials can never weld two different people together.
    if len(lf) >= 3 and len(rf) >= 3 and (lf.startswith(rf) or rf.startswith(lf)):
        return True
    return False
