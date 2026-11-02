"""Which political party a member belongs to, per chamber, from the sources' own records.

Sněmovna: the poslanecký klub (clubs there are formed per party).
Senát:    "politická příslušnost" on the senator's senat.cz profile (party
          membership; Senate clubs often join several parties, so they are
          not used for this).
EP:       the national party the European Parliament records for the MEP.

Sources spell the same party differently (ODS / Občanská demokratická strana /
Poslanecký klub Občanské demokratické strany). PARTIES lists those spellings so
they land on one page; nothing else is merged. A name not listed here gets its
own page under the name the source uses.
"""
from __future__ import annotations

import re

from .db import fold

# key, short name, full name, other spellings used by the sources
PARTIES = [
    ("ano", "ANO", "ANO 2011", ["ANO2011", "ANO 2011", "Poslanecký klub ANO 2011", "ANO"]),
    ("ods", "ODS", "Občanská demokratická strana", ["ODS", "Občanské demokratické strany"]),
    ("stan", "STAN", "Starostové a nezávislí", ["STAN", "Starostové a nezávislí", "Starostove a nezavisli"]),
    ("pirati", "Piráti", "Česká pirátská strana", ["Piráti", "PIR", "Pirátská strana", "České pirátské strany", "Česká pirátská strana"]),
    ("kdu-csl", "KDU-ČSL", "Křesťanská a demokratická unie – Československá strana lidová",
     ["KDU-ČSL", "KDU", "Křesťanská a demokratická unie - Československá strana lidová"]),
    ("top-09", "TOP 09", "TOP 09", ["TOP09", "TOP 09"]),
    ("spd", "SPD", "Svoboda a přímá demokracie", ["SPD", "Svoboda a přímá demokracie",
                                                   "Svoboda a přímá demokracie - Tomio Okamura"]),
    ("motoriste", "Motoristé", "Motoristé sobě", ["Motoristé sobě", "Motoristé", "MS", "Moto", "AUTO"]),
    ("kscm", "KSČM", "Komunistická strana Čech a Moravy", ["KSČM", "Komunistická strana Čech a Moravy"]),
    ("socdem", "SOCDEM", "Sociální demokracie", ["SOCDEM", "ČSSD", "Česká strana sociálně demokratická",
                                                 "Sociální demokracie"]),
    ("prisaha", "Přísaha", "PŘÍSAHA – občanské hnutí Roberta Šlachty",
     ["Přísaha", "PŘÍSAHA", "PSH", "PŘÍSAHA - občanské hnutí Roberta Šlachty", "Přísaha - občanské hnutí Roberta Šlachty"]),
    ("sen-21", "SEN 21", "Senátor 21", ["SEN 21", "SEN21", "Senátor 21"]),
    ("zeleni", "Zelení", "Strana zelených", ["Zelení", "Strana zelených", "SZ"]),
    ("slk", "SLK", "Starostové pro Liberecký kraj", ["SLK", "Starostové pro Liberecký kraj"]),
    ("svobodni", "Svobodní", "Strana svobodných občanů", ["Svobodní", "Strana svobodných občanů"]),
]

# not a party: independents and placeholders the sources use
NO_PARTY = {"nezarazeni", "nezaraz", "bez politicke prislusnosti", "bezpp", "ind", "nestran", "bez prislusnosti", "nestranik", "nestranicka",
            "independent", "non-attached", "-"}

_PREFIX = re.compile(r"^(poslanecky klub|senatorsky klub)\s+")
_ALIAS = {}
INFO = {}
for _key, _abbr, _name, _aliases in PARTIES:
    INFO[_key] = (_abbr, _name)
    for _a in [_abbr, _name, *_aliases]:
        _ALIAS[_PREFIX.sub("", fold(_a))] = _key


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(s)).strip("-")


def party_of(name: str | None, abbr: str | None = None) -> tuple[str, str, str] | None:
    """(key, short name, full name) for a source's club or party label, or None when it isn't a party."""
    if not name and not abbr:
        return None
    labels = [x for x in (name, abbr) if x]
    folded = [_PREFIX.sub("", fold(x)).rstrip(".") for x in labels]
    if any(f in NO_PARTY or f.startswith("bez politicke") for f in folded):
        return None
    for f in folded:
        if f in _ALIAS:
            k = _ALIAS[f]
            return (k, *INFO[k])
    full = re.sub(r"^(Poslanecký klub|Senátorský klub)\s+", "", name or abbr).strip()
    short = abbr if abbr and len(abbr) < len(full) else full
    return (slug(full), short, full)
