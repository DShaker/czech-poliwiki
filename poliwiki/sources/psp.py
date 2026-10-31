"""Poslanecká sněmovna open data (https://www.psp.cz/sqw/hp.sqw?k=1300).

Reads the zipped UNL dumps (pipe-separated, cp1250) as downloaded by
scripts/fetch_raw.sh: poslanci.zip, hl-<term>ps.zip, tisky.zip, schuze.zip.

Vote codes in hl<term>h*.unl (psp.cz documentation):
  A yes | B, N no | C abstain | K abstained or did not vote (since 2013 the
  voting system cannot tell these apart) | F did not vote | @ not logged in
  | M excused | W voted before taking the oath.
"""
from __future__ import annotations

import io
import logging
import sqlite3
import zipfile
from collections import defaultdict
from pathlib import Path

from ..db import Run, resolve_person

log = logging.getLogger(__name__)
BASE_URL = "https://www.psp.cz"

VOTE_CODES = {
    "A": "yes", "B": "no", "N": "no", "C": "abstain", "K": "abstain",
    "F": "not_voting", "W": "not_voting", "@": "absent", "M": "excused",
}
OUTCOMES = {"A": "passed", "R": "rejected"}
TYP_KLUB = "1"
TYP_SNEMOVNA = "11"
TYP_KRAJ = "8"


def read_unl(zf: zipfile.ZipFile, name: str):
    """Yield rows of a UNL member as lists of str (trailing empty field dropped)."""
    with zf.open(name) as fh:
        for line in io.TextIOWrapper(fh, encoding="cp1250", newline="\n"):
            line = line.rstrip("\r\n")
            if not line:
                continue
            parts = line.split("|")
            if parts and parts[-1] == "":
                parts.pop()
            yield parts


def col(row: list[str], i: int) -> str | None:
    v = row[i].strip() if i < len(row) else ""
    return v or None


def cz_date(s: str | None) -> str | None:
    """'03.11.2025' -> '2025-11-03'; '2025-10-04 00' -> '2025-10-04'."""
    if not s:
        return None
    s = s.strip()
    if len(s) >= 10 and s[2] == "." and s[5] == ".":
        return f"{s[6:10]}-{s[3:5]}-{s[0:2]}"
    return s[:10] if len(s) >= 10 and s[4] == "-" else None


class PspData:
    def __init__(self, raw: str | Path, term: str):
        self.raw = Path(raw)
        self.term = str(term)
        with zipfile.ZipFile(self.raw / "poslanci.zip") as zf:
            self.organy = {r[0]: r for r in read_unl(zf, "organy.unl")}
            self.osoby = {r[0]: r for r in read_unl(zf, "osoby.unl")}
            self.poslanec = list(read_unl(zf, "poslanec.unl"))
            self.zarazeni = list(read_unl(zf, "zarazeni.unl"))
        self.term_organ = self._term_organ()

    def _term_organ(self) -> str:
        for oid, r in self.organy.items():
            if r[2] == TYP_SNEMOVNA and (cz_date(col(r, 6)) or "").startswith(self.term):
                return oid
        raise ValueError(f"no PSP term starting in {self.term}")


def ingest(conn: sqlite3.Connection, raw: str | Path, term: str = "2025") -> None:
    with Run(conn, f"psp{term}") as run:
        d = PspData(raw, term)
        poslanci = ingest_members(conn, d)
        n_bills = ingest_bills(conn, d)
        n_votes = ingest_votes(conn, d, poslanci)
        run.detail = f"{len(set(poslanci.values()))} MPs, {n_bills} documents, {n_votes} votes"
        log.info("PSP %s: %s", term, run.detail)


def _person(conn, d: PspData, id_osoba: str) -> int:
    r = d.osoby[id_osoba]
    gender = {"M": "M", "Z": "F", "Ž": "F"}.get(col(r, 6) or "")
    return resolve_person(
        conn, "psp", id_osoba,
        given_name=col(r, 3), family_name=col(r, 2),
        title_before=col(r, 1), title_after=col(r, 4),
        birth_date=_birth(col(r, 5)), gender=gender, country="CZE",
        url=f"{BASE_URL}/sqw/detail.sqw?id={id_osoba}",
    )


def _birth(s: str | None) -> str | None:
    d = cz_date(s)
    return None if d == "1900-01-01" else d  # psp.cz placeholder for unknown


def ingest_members(conn: sqlite3.Connection, d: PspData) -> dict[str, int]:
    """Create MPs of the term with mandates and club memberships.

    Returns {id_poslanec: person_id}.
    """
    term_org = d.term_organ
    out: dict[str, int] = {}
    seat_dates = defaultdict(list)
    for z in d.zarazeni:
        if z[1] == term_org and z[2] == "0":
            seat_dates[z[0]].append((cz_date(col(z, 3)), cz_date(col(z, 4))))
    clubs = {oid for oid, r in d.organy.items() if r[2] == TYP_KLUB and r[1] == term_org}

    for p in d.poslanec:
        if p[4] != term_org:
            continue
        id_pos, id_osoba = p[0], p[1]
        pid = out[id_pos] = _person(conn, d, id_osoba)
        kraj = d.organy.get(p[2])
        kraj_name = kraj[4] if kraj else None
        for start, end in seat_dates.get(id_osoba) or [(None, None)]:
            conn.execute(
                "INSERT OR IGNORE INTO mandate (person_id, chamber, term, start_date, end_date, constituency, source_ref)"
                " VALUES (?,?,?,?,?,?,?)",
                (pid, "psp", d.term, start, end, kraj_name, f"poslanec:{id_pos}"),
            )
        kand = d.organy.get(p[3])
        if kand:
            conn.execute(
                "INSERT OR IGNORE INTO affiliation (person_id, chamber, kind, org_id, org_name, org_abbr, start_date, end_date)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (pid, "psp", "candidate_list", f"psp:{kand[0]}", kand[4], col(kand, 3),
                 (seat_dates.get(id_osoba) or [(None, None)])[0][0], None),
            )
    for z in d.zarazeni:
        if z[1] in clubs and z[2] == "0":
            id_osoba = z[0]
            pid = conn.execute(
                "SELECT person_id FROM person_source WHERE source='psp' AND source_id=?", (id_osoba,)
            ).fetchone()
            if not pid:
                continue
            org = d.organy[z[1]]
            conn.execute(
                "INSERT OR IGNORE INTO affiliation (person_id, chamber, kind, org_id, org_name, org_abbr, start_date, end_date)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (pid[0], "psp", "club", f"psp:{org[0]}", org[4], col(org, 3),
                 cz_date(col(z, 3)), cz_date(col(z, 4))),
            )
    conn.commit()
    return out


def ingest_bills(conn: sqlite3.Connection, d: PspData) -> int:
    """Parliamentary prints ("sněmovní tisky") of the term and their sponsors."""
    with zipfile.ZipFile(d.raw / "tisky.zip") as zf:
        druh = {r[0]: (r[1], r[2]) for r in read_unl(zf, "druh_tisku.unl")}
        sponsors = defaultdict(list)
        for r in read_unl(zf, "predkladatel.unl"):
            sponsors[r[0]].append((int(r[2] or 0), r[1]))
        tisky = [r for r in read_unl(zf, "tisky.unl") if col(r, 7) == d.term_organ]
    psp_people = {
        sid: pid for sid, pid in conn.execute("SELECT source_id, person_id FROM person_source WHERE source='psp'")
    }
    n = 0
    for r in tisky:
        id_tisk, id_druh, cislo = r[0], r[1], r[3]
        kind, label = druh.get(id_druh, ("", None))
        if kind != "T":  # skip amendments, opinions etc. attached to a main print
            continue
        sp = [psp_people.get(o) for _, o in sorted(sponsors.get(id_tisk, []))]
        sp = [s for s in sp if s]
        proposer = _proposer_kind(col(r, 5), id_druh, col(r, 9))
        bid = f"psp:{d.term}:{cislo}"
        conn.execute(
            "INSERT OR REPLACE INTO bill (id, chamber, term, number, title, bill_type, submitted_date, proposer_kind, url)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (bid, "psp", d.term, cislo, col(r, 15) or col(r, 10), label, cz_date(col(r, 11)), proposer,
             f"{BASE_URL}/sqw/historie.sqw?o={_obdobi_no(d)}&t={cislo}"),
        )
        conn.execute("DELETE FROM bill_sponsor WHERE bill_id=?", (bid,))
        if sp:
            conn.executemany(
                "INSERT OR IGNORE INTO bill_sponsor (bill_id, person_id, sponsor_text) VALUES (?,?,'')",
                [(bid, pid) for pid in sp],
            )
        else:
            conn.execute(
                "INSERT INTO bill_sponsor (bill_id, person_id, sponsor_text) VALUES (?,NULL,?)",
                (bid, col(r, 9) or ""),
            )
        n += 1
    conn.commit()
    return n


def _proposer_kind(id_navrh: str | None, id_druh: str, text: str | None) -> str:
    """Classify who submitted a print, from tisky.id_navrh.

    Codes observed in the data (psp.cz does not publish the lookup table):
    1 government, 2 one MP, 3 group of MPs, 4 Senát, 6 senator(s),
    7+ a regional assembly, 0 other (reports, ČNB, ministries' documents).
    """
    n = int(id_navrh or 0)
    if n == 1:
        return "government"
    if n in (2, 3):
        return "mps"
    if n in (4, 5, 6):
        return "senate"
    if n >= 7:
        return "region"
    t = (text or "").lower()
    if id_druh in ("3", "4") or t.startswith(("min", "vláda", "předseda vlády")):
        return "government"
    return "other"


def _obdobi_no(d: PspData) -> str:
    return (d.organy[d.term_organ][3] or "PSP").removeprefix("PSP")


def _agenda_bills(d: PspData) -> dict[tuple[str, str], str]:
    """(schuze number, bod number) -> tisk number, for this term."""
    with zipfile.ZipFile(d.raw / "schuze.zip") as zf:
        schuze = {r[0]: r[2] for r in read_unl(zf, "schuze.unl") if r[1] == d.term_organ}
        with zipfile.ZipFile(d.raw / "tisky.zip") as zt:
            tisk_no = {r[0]: r[3] for r in read_unl(zt, "tisky.unl") if col(r, 7) == d.term_organ}
        out = {}
        for r in read_unl(zf, "bod_schuze.unl"):
            if r[1] in schuze and col(r, 2) and col(r, 4) and r[2] in tisk_no:
                out[(schuze[r[1]], r[4])] = tisk_no[r[2]]
    return out


def ingest_votes(conn: sqlite3.Connection, d: PspData, poslanci: dict[str, int]) -> int:
    zip_path = d.raw / f"hl-{d.term}ps.zip"
    agenda = _agenda_bills(d)
    bills = {r[0] for r in conn.execute("SELECT id FROM bill WHERE chamber='psp' AND term=?", (d.term,))}
    obdobi = _obdobi_no(d)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        void = {r[0] for r in read_unl(zf, "zmatecne.unl")} if "zmatecne.unl" in names else set()
        excuses = defaultdict(list)
        if "omluvy.unl" in names:
            for r in read_unl(zf, "omluvy.unl"):
                if r[0] == d.term_organ:
                    excuses[(r[1], cz_date(r[2]))].append((col(r, 3), col(r, 4)))
        events = {}
        for r in read_unl(zf, f"hl{d.term}s.unl"):
            if r[1] != d.term_organ:
                continue
            day = cz_date(r[5])
            tisk = agenda.get((r[2], r[4]))
            bill_id = f"psp:{d.term}:{tisk}" if tisk and f"psp:{d.term}:{tisk}" in bills else None
            outcome = "void" if r[0] in void else OUTCOMES.get(col(r, 14) or "", "other")
            events[r[0]] = (day, col(r, 6))
            conn.execute(
                "INSERT OR REPLACE INTO vote_event (id, chamber, term, date, time, session, number, title, title_detail,"
                " outcome, yes_count, no_count, abstain_count, present_count, quorum, bill_id, url)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"psp:{d.term}:{r[0]}", "psp", d.term, day, col(r, 6), r[2], r[3],
                 col(r, 15) or col(r, 16), col(r, 16), outcome,
                 _int(r[7]), _int(r[8]), _int(r[9]), _int(r[11]), _int(r[12]), bill_id,
                 f"{BASE_URL}/sqw/hlasy.sqw?g={r[0]}"),
            )
        h_files = sorted(n for n in names if n.startswith(f"hl{d.term}h"))
        batch = []
        for name in h_files:
            for r in read_unl(zf, name):
                id_pos, id_hl, code = r[0], r[1], (col(r, 2) or "")
                if id_hl not in events or id_pos not in poslanci:
                    continue
                opt = VOTE_CODES.get(code, "other")
                if opt == "absent" and _excused(excuses.get((id_pos, events[id_hl][0])), events[id_hl][1]):
                    opt = "excused"
                batch.append((f"psp:{d.term}:{id_hl}", poslanci[id_pos], opt))
                if len(batch) >= 50_000:
                    _flush(conn, batch)
        _flush(conn, batch)
    conn.commit()
    return len(events)


def _excused(windows, time: str | None) -> bool:
    if not windows:
        return False
    for start, end in windows:
        if time is None or ((start or "00:00") <= time <= (end or "23:59")):
            return True
    return False


def _flush(conn, batch: list) -> None:
    conn.executemany("INSERT OR REPLACE INTO vote (vote_event_id, person_id, option) VALUES (?,?,?)", batch)
    batch.clear()


def _int(s: str | None) -> int | None:
    try:
        return int(s) if s not in (None, "") else None
    except ValueError:
        return None
