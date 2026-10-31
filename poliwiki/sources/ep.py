"""European Parliament open data (https://data.europarl.europa.eu/en/developer-corner).

Ingests, for one parliamentary term:
  * Czech MEPs with birth date, mandate periods, EP group and national party;
  * every roll-call vote (all MEPs, so the spectrum model in step 3 can place
    Czech MEPs relative to the whole chamber);
  * per-MEP vote options, incl. 'excused', 'not_voting' (attended that day but
    did not vote) and, for Czech MEPs, 'absent'.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date
from urllib.parse import urlencode

from .. import http
from ..db import Run, resolve_person

log = logging.getLogger(__name__)
API = "https://data.europarl.europa.eu/api/v2"
TERM_YEARS = {9: range(2019, 2025), 10: range(2024, date.today().year + 1)}
TERM_START = {9: "2019-07-02", 10: "2024-07-16"}
OUTCOME = {"ADOPTED": "passed", "REJECTED": "rejected"}


def _get(path: str, cache: bool = True, **params):
    params = {"format": "application/ld+json", **params}
    try:
        return http.get_json(f"{API}/{path}?{urlencode(params)}", cache=cache).get("data", [])
    except http.NotFound:
        log.debug("404 %s %s", path, params)
        return []


def _paged(path: str, page: int = 500, cache: bool = True, **params):
    """Page through a listing. Some endpoints ignore limit/offset and return
    everything at once, so stop as soon as a page brings no new ids."""
    offset, seen = 0, set()
    while True:
        batch = [x for x in _get(path, cache=cache, limit=page, offset=offset, **params) if x["id"] not in seen]
        if not batch:
            return
        seen.update(x["id"] for x in batch)
        yield from batch
        offset += page


def _pid(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _org_label(org_id: str, _cache: dict = {}) -> tuple[str, str | None]:
    if org_id not in _cache:
        d = _get(f"corporate-bodies/{org_id}")
        d = d[0] if d else {}
        alt = d.get("altLabel", {})
        _cache[org_id] = (alt.get("cs") or alt.get("en") or d.get("label") or org_id, d.get("label"))
    return _cache[org_id]


def ingest_meps(conn: sqlite3.Connection, term: int) -> dict[str, int]:
    """Create persons for every MEP of the term; full detail for Czech MEPs."""
    ids: dict[str, int] = {}
    # listings and Czech MEPs' details change; never serve them from the cache
    cz = {m["identifier"] for m in _paged("meps", cache=False, **{"parliamentary-term": term, "country-of-representation": "CZ"})}
    for m in _paged("meps", cache=False, **{"parliamentary-term": term}):
        mid = m["identifier"]
        url = f"https://www.europarl.europa.eu/meps/en/{mid}"
        if mid not in cz:
            pid = ids[mid] = resolve_person(conn, "ep", mid, given_name=m.get("givenName"),
                                            family_name=m.get("familyName"), url=url)
            # EP group of every MEP: needed for group majorities and to read the spectrum
            for ms in (_get(f"meps/{mid}") or [{}])[0].get("hasMembership", []):
                if not ms.get("membershipClassification", "").endswith("EU_POLITICAL_GROUP"):
                    continue
                period = ms.get("memberDuring", {})
                start, end = period.get("startDate"), period.get("endDate")
                if end and end < TERM_START[term]:
                    continue
                oid = _pid(ms.get("organization", ""))
                name, abbr = _org_label(oid)
                conn.execute(
                    "INSERT OR IGNORE INTO affiliation (person_id, chamber, kind, org_id, org_name, org_abbr, start_date, end_date)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (pid, "ep", "ep_group", oid, name, abbr, start, end),
                )
            continue
        d = _get(f"meps/{mid}", cache=False)[0]
        gender = {"MALE": "M", "FEMALE": "F"}.get(d.get("hasGender", "").rsplit("/", 1)[-1])
        pid = resolve_person(conn, "ep", mid, given_name=d.get("givenName"),
                             family_name=d.get("familyName"), birth_date=d.get("bday"),
                             gender=gender, country="CZE", url=url)
        ids[mid] = pid
        for ms in d.get("hasMembership", []):
            period = ms.get("memberDuring", {})
            start, end = period.get("startDate"), period.get("endDate")
            org = ms.get("organization", "")
            klass = ms.get("membershipClassification", "").rsplit("/", 1)[-1]
            if ms.get("role", "").endswith("MEMBER_PARLIAMENT") and org.startswith("org/ep-"):
                conn.execute(
                    "INSERT OR IGNORE INTO mandate (person_id, chamber, term, start_date, end_date, constituency, source_ref)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (pid, "ep", org.split("-")[-1], start, end, "CZ", ms.get("identifier")),
                )
            elif klass in ("EU_POLITICAL_GROUP", "NATIONAL_POLITICAL_GROUP"):
                oid = _pid(org)
                name, abbr = _org_label(oid)
                kind = "ep_group" if klass == "EU_POLITICAL_GROUP" else "national_party"
                conn.execute(
                    "INSERT OR IGNORE INTO affiliation (person_id, chamber, kind, org_id, org_name, org_abbr, start_date, end_date)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (pid, "ep", kind, oid, name, abbr, start, end),
                )
    conn.commit()
    log.info("EP%s: %d MEPs (%d Czech)", term, len(ids), len(cz))
    return ids


def _title(item: dict) -> str | None:
    lab = item.get("activity_label") or {}
    return lab.get("cs") or lab.get("en") or lab.get("mul") or next(iter(lab.values()), None)


def ingest_votes(conn: sqlite3.Connection, term: int, meps: dict[str, int], since: str | None = None) -> int:
    """Ingest all plenary roll-call votes of a term. Returns number of vote events."""
    cz_mandates = conn.execute(
        "SELECT m.person_id, m.start_date, m.end_date FROM mandate m WHERE chamber='ep' AND term=?",
        (str(term),),
    ).fetchall()
    since = since or TERM_START[term]
    today = date.today().isoformat()
    n = 0
    for year in TERM_YEARS[term]:
        # the current year's meeting list grows; past years are fixed
        for mtg in _get("meetings", cache=year < date.today().year, year=year, limit=500):
            day = mtg["activity_date"]
            if day < since or not mtg["activity_id"].startswith("MTG-PL"):
                continue
            # past meetings are immutable; re-fetch the last week to pick up late corrections
            cache = (date.fromisoformat(today) - date.fromisoformat(day)).days > 7
            n += _ingest_meeting(conn, term, mtg, meps, cz_mandates, cache)
        conn.commit()
    return n


def _ingest_meeting(conn, term, mtg, meps, cz_mandates, cache) -> int:
    mid, day = mtg["activity_id"], mtg["activity_date"]
    participants = {_pid(p) for p in mtg.get("had_participant_person", [])}
    excused = {_pid(p) for p in mtg.get("had_excused_person", [])}
    titles: dict[str, tuple[str | None, str | None]] = {}
    for item in _paged(f"meetings/{mid}/vote-results", cache=cache):
        docs = item.get("based_on_a_realization_of") or []
        for dec in item.get("consists_of", []):
            titles[_pid(dec)] = (_title(item), docs[0].rsplit("/", 1)[-1] if docs else None)
    n = 0
    for dec in _paged(f"meetings/{mid}/decisions", cache=cache):
        if not dec.get("decision_method", "").endswith("ROLLCALL"):
            continue
        vid = dec.get("notation_votingId") or dec["activity_id"]
        ev = f"ep:{vid}"
        parent_title, doc = titles.get(dec["activity_id"], (None, None))
        conn.execute(
            "INSERT OR REPLACE INTO vote_event (id, chamber, term, date, time, session, number, title, title_detail,"
            " outcome, yes_count, no_count, abstain_count, present_count, doc_ref, url)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev, "ep", str(term), day, (dec.get("activity_start_date") or "")[11:19] or None, mid,
             dec.get("activity_order"), parent_title, _title(dec),
             OUTCOME.get(dec.get("decision_outcome", "").rsplit("/", 1)[-1], "other"),
             dec.get("number_of_votes_favor"), dec.get("number_of_votes_against"),
             dec.get("number_of_votes_abstention"), dec.get("number_of_attendees"), doc,
             f"https://data.europarl.europa.eu/{dec['id']}"),
        )
        options: dict[str, str] = {}
        for key, opt in (("had_voter_favor", "yes"), ("had_voter_against", "no"), ("had_voter_abstention", "abstain")):
            for p in dec.get(key, []):
                options[_pid(p)] = opt
        for p in participants - options.keys():
            options[p] = "not_voting"
        for p in excused - options.keys():
            options[p] = "excused"
        rows = []
        for mep, opt in options.items():
            pid = meps.get(mep)
            if pid is None:  # MEP not in the term list (rare data gaps): create a bare person
                pid = meps[mep] = resolve_person(conn, "ep", mep, given_name=None, family_name=None)
            rows.append((ev, pid, opt))
        voted = {pid for _, pid, _ in rows}
        for pid, start, end in cz_mandates:
            if pid not in voted and start <= day and (end is None or day <= end):
                rows.append((ev, pid, "absent"))
        conn.execute("DELETE FROM vote WHERE vote_event_id=?", (ev,))
        conn.executemany("INSERT INTO vote (vote_event_id, person_id, option) VALUES (?,?,?)", rows)
        n += 1
    return n


def ingest(conn: sqlite3.Connection, term: int = 10, since: str | None = None) -> None:
    with Run(conn, f"ep{term}") as run:
        meps = ingest_meps(conn, term)
        n = ingest_votes(conn, term, meps, since)
        run.detail = f"{len(meps)} MEPs, {n} roll-call votes"
        log.info("EP%s: %s", term, run.detail)
