"""Senát: senators, club memberships and individual votes.

Identity, birth dates, mandates and club memberships come from the psp.cz
open-data dump (poslanci.zip), which also records the Senate (organ type 79,
clubs type 90). Votes come from senat.cz's official per-senator RSS feed
(/senatori/hlasovani_rss.php?pid=<id>), which lists every vote the senator
took part in with their option and the result. The vote database pages under
/xqw/xervlet/ are not used: they are behind a firewall that rejects
non-browser clients.
"""
from __future__ import annotations

import html
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import http
from ..db import Run, fold, merge_persons, resolve_person
from .psp import PspData, _person, col, cz_date

log = logging.getLogger(__name__)
SENAT = "https://www.senat.cz"
TYP_SENAT = "79"
TYP_SEN_KLUB = "90"
PRAGUE = ZoneInfo("Europe/Prague")
OPTIONS = {"pro": "yes", "proti": "no", "zdržel se": "abstain", "zdržela se": "abstain",
           "nepřítomen": "absent", "nepřítomna": "absent", "nehlasoval": "not_voting",
           "nehlasovala": "not_voting", "omluven": "excused", "omluvena": "excused"}
OUTCOMES = {"Návrh byl přijat": "passed", "Návrh byl zamítnut": "rejected", "Zmatečné hlasování": "void"}
DESC_RE = re.compile(r"^\s*(\d+)\s+-\s+(.*)\s+-\s+([^;-]+?)\s*;\s*Senát odhlasoval:\s*(.*?)\s*$", re.S)
PRINT_RE = re.compile(r"^(N\s+)?\d+/\d+$")


def current_term() -> int:
    """The Senate term (funkční období) senat.cz currently shows, e.g. 15."""
    page = http.get(f"{SENAT}/senatori/index.php?lng=cz&par_2=2", cache=False).decode("utf-8", "replace")
    return max(int(t) for t in re.findall(r"[?&](?:amp;)?O=(\d+)", page))


def ingest(conn: sqlite3.Connection, psp_raw: str | Path, term: int | None = None, refresh: bool = True) -> None:
    term = term or current_term()
    with Run(conn, f"senat{term}") as run:
        d = PspData(psp_raw, "2025")  # any term: only organy/osoby/zarazeni are used
        senators = ingest_members(conn, d, term)
        n_ev, n_votes = 0, 0
        for pid_web, person_id in senators.items():
            e, v = ingest_votes(conn, pid_web, person_id, term, refresh)
            n_ev, n_votes = max(n_ev, e), n_votes + v
        conn.commit()
        total = conn.execute("SELECT count(*) FROM vote_event WHERE chamber='senat' AND term=?", (str(term),)).fetchone()[0]
        run.detail = f"{len(senators)} senators, {total} vote events, {n_votes} individual votes"
        log.info("Senát %s: %s", term, run.detail)


def current_senators() -> list[tuple[str, str]]:
    """[(senat.cz id, display name)] from the official list page."""
    page = http.get(f"{SENAT}/senatori/index.php?lng=cz&par_2=2", cache=False).decode("utf-8", "replace")
    seen, out = set(), []
    for pid, name in re.findall(r'href="[^"]*par_3=(\d+)[^"]*"[^>]*>(.*?)</a>', page, re.S):
        name = html.unescape(re.sub(r"<[^>]+>", "", name)).replace("\xa0", " ").strip()
        if pid not in seen and name:
            seen.add(pid)
            out.append((pid, name))
    return out


def ingest_members(conn: sqlite3.Connection, d: PspData, term: int) -> dict[str, int]:
    """Return {senat.cz id: person_id} for senators of the term."""
    se = sorted((r for r in d.organy.values() if r[2] == TYP_SENAT and (r[3] or "").startswith("SE")),
                key=lambda r: int(r[3][2:]))
    exact = [r for r in se if r[3] == f"SE{term}"]
    organ = (exact or se[-1:])[0][0]  # psp.cz may not have the newest Senate term yet
    seats = defaultdict(list)
    for z in d.zarazeni:
        if z[1] == organ and z[2] == "0":
            seats[z[0]].append((cz_date(col(z, 3)), cz_date(col(z, 4))))
    by_name = defaultdict(list)
    for id_osoba in seats:
        r = d.osoby[id_osoba]
        by_name[(fold(col(r, 3)), fold(col(r, 2)))].append(id_osoba)

    out: dict[str, int] = {}
    for pid_web, name in current_senators():
        ids = [i for (g, f), m in by_name.items() if fold(name) == f"{g} {f}" for i in m]
        url = f"{SENAT}/senatori/index.php?lng=cz&par_3={pid_web}"
        existing = conn.execute(
            "SELECT person_id FROM person_source WHERE source='senat' AND source_id=?", (pid_web,)
        ).fetchone()
        if len(ids) != 1:
            # psp.cz lags behind new Senate terms; keep the senator without a birth date
            # (so no cross-chamber link) until the dump catches up.
            log.warning("Senator %s (%s): %d psp.cz identity matches, using senat.cz only", name, pid_web, len(ids))
            given, _, family = name.partition(" ")
            out[pid_web] = existing[0] if existing else resolve_person(
                conn, "senat", pid_web, given_name=given, family_name=family, country="CZE", url=url)
            _profile_rows(conn, out[pid_web], pid_web, term)
            continue
        id_osoba = ids[0]
        person_id = _person(conn, d, id_osoba)
        if existing and existing[0] != person_id:
            merge_persons(conn, person_id, existing[0])
        conn.execute(
            "INSERT OR IGNORE INTO person_source (source, source_id, person_id, url) VALUES ('senat',?,?,?)",
            (pid_web, person_id, url),
        )
        out[pid_web] = person_id
        _profile_rows(conn, person_id, pid_web, term)
        for z in d.zarazeni:
            if z[0] == id_osoba and z[2] == "0" and d.organy.get(z[1], [None] * 3)[2] == TYP_SEN_KLUB:
                org = d.organy[z[1]]
                conn.execute(
                    "INSERT OR IGNORE INTO affiliation (person_id, chamber, kind, org_id, org_name, org_abbr, start_date, end_date)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (person_id, "senat", "club", f"psp:{org[0]}", org[4], col(org, 3),
                     cz_date(col(z, 3)), cz_date(col(z, 4))),
                )
    conn.commit()
    return out


def parse_profile(page: str) -> dict:
    """Party, constituency, nominating list and mandate dates from a profile page."""
    page = re.sub(r"<!--.*?-->", "", page, flags=re.S)
    t = html.unescape(re.sub(r"<[^>]+>", " ", page)).replace("\xa0", " ")
    t = re.sub(r"\s+", " ", t)
    out = {}
    if m := re.search(r"Politická příslušnost (.+?) Obvod", t):
        out["party"] = m.group(1).strip()
    if m := re.search(r"Obvod č\. (\d+)", t):
        out["district"] = m.group(1)
    if m := re.search(r"Zvolen za (.+?) v roce (\d{4})", t):
        out["elected_for"], out["elected_year"] = m.group(1).strip(), m.group(2)
    if m := re.search(r"Mandát (\d\d\.\d\d\.\d{4}) - (\d\d\.\d\d\.\d{4})?", t):
        out["start"], out["end"] = cz_date(m.group(1)), cz_date(m.group(2))
    return out


def _profile_rows(conn, person_id: int, pid_web: str, term: int) -> None:
    page = http.get(f"{SENAT}/senatori/index.php?lng=cz&par_3={pid_web}", cache=False).decode("utf-8", "replace")
    p = parse_profile(page)
    if not p.get("start"):
        log.warning("senator %s: no mandate on profile page", pid_web)
        return
    conn.execute(
        "INSERT OR IGNORE INTO mandate (person_id, chamber, term, start_date, end_date, constituency, source_ref)"
        " VALUES (?,?,?,?,?,?,?)",
        (person_id, "senat", str(term), p["start"], p.get("end"),
         f"obvod {p['district']}" if p.get("district") else None, f"senat:{pid_web}"),
    )
    for kind, name in (("party", p.get("party")), ("candidate_list", p.get("elected_for"))):
        if name:
            conn.execute(
                "INSERT OR IGNORE INTO affiliation (person_id, chamber, kind, org_id, org_name, org_abbr, start_date, end_date)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (person_id, "senat", kind, None, name, name, p["start"], None),
            )


def parse_feed(xml_bytes: bytes):
    """Yield dicts for each vote in a senator's RSS feed."""
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):
        guid = (it.findtext("guid") or "").strip()
        title = (it.findtext("title") or "").strip()
        desc = (it.findtext("description") or "").strip()
        m = DESC_RE.match(desc)
        parts = [p.strip() for p in title.split(" - ", 4)]
        if not m or len(parts) < 4:
            log.debug("unparsed feed item %s: %r", guid, desc[:80])
            continue
        if len(parts) == 5 and PRINT_RE.match(parts[3]):
            print_no, subject = parts[3], parts[4]
        else:
            print_no, subject = None, " - ".join(parts[3:])
        ts = parsedate_to_datetime(it.findtext("pubDate")).astimezone(PRAGUE)
        yield {
            "id": guid.removeprefix("SENAT_PCR_H"),
            "term": parts[0], "session": parts[1], "number": parts[2],
            "print": print_no, "title": subject,
            "motion": m.group(2).strip(), "option": OPTIONS.get(m.group(3).strip(), "other"),
            "outcome": OUTCOMES.get(m.group(4).strip(), "other"),
            "date": ts.date().isoformat(), "time": ts.strftime("%H:%M"),
        }


def ingest_votes(conn, pid_web: str, person_id: int, term: int, refresh: bool) -> tuple[int, int]:
    url = f"{SENAT}/senatori/hlasovani_rss.php?pid={pid_web}"
    items = None
    for attempt in range(3):
        try:
            items = list(parse_feed(http.get(url, cache=not refresh and attempt == 0)))
            break
        except ET.ParseError as e:  # truncated transfer: fetch again
            log.warning("feed %s unreadable (%s), attempt %d", pid_web, e, attempt + 1)
    if items is None:
        log.error("feed %s skipped after 3 attempts", pid_web)
        return 0, 0
    n_ev = n = 0
    for v in items:
        if v["term"] != str(term):
            continue
        # the feed guid is per senator; a vote is identified by term, session and number
        ev = f"senat:{term}:{v['session']}:{v['number']}"
        bill_id = None
        if v["print"]:
            bill_id = f"senat:{term}:{v['print'].replace(' ', '')}"
            conn.execute(
                "INSERT OR IGNORE INTO bill (id, chamber, term, number, title, url) VALUES (?,?,?,?,?,?)",
                (bill_id, "senat", str(term), v["print"], v["title"],
                 f"{SENAT}/xqw/xervlet/pssenat/historie?action=detail&value={v['print'].split('/')[0].replace('N ', 'N')}"),
            )
        cur = conn.execute(
            "INSERT OR IGNORE INTO vote_event (id, chamber, term, date, time, session, number, title, title_detail,"
            " outcome, bill_id, url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev, "senat", str(term), v["date"], v["time"], v["session"], v["number"], v["title"],
             v["motion"], v["outcome"], bill_id,
             f"{SENAT}/xqw/xervlet/pssenat/hlasy?O={term}&S={v['session']}"),
        )
        n_ev += cur.rowcount
        conn.execute(
            "INSERT OR REPLACE INTO vote (vote_event_id, person_id, option) VALUES (?,?,?)",
            (ev, person_id, v["option"]),
        )
        n += 1
    return n_ev, n
