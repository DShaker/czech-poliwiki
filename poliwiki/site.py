"""Static site: one profile page per Czech politician, chamber spectrum pages,
methodology. Output is plain HTML/CSS/JS plus JSON for the vote lists, so it
can be served from GitHub Pages as is.

    python -m poliwiki site --out site
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import statistics
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape as e
from importlib import resources
from pathlib import Path

from .db import fold

log = logging.getLogger(__name__)

CHAMBER = {"psp": "Poslanecká sněmovna", "senat": "Senát", "ep": "Evropský parlament"}
CHAMBER_SHORT = {"psp": "Sněmovna", "senat": "Senát", "ep": "EP"}
CLUB_KIND = {"psp": "club", "senat": "club", "ep": "ep_group"}
AFF_LABEL = {
    "club": "poslanecký / senátorský klub", "ep_group": "politická skupina EP",
    "national_party": "národní strana", "party": "politická příslušnost",
    "candidate_list": "zvolen/a za",
}
OPT_CODE = {"yes": "y", "no": "n", "abstain": "a", "not_voting": "v", "absent": "x", "excused": "e"}
OPT_LABEL = {"yes": "pro", "no": "proti", "abstain": "zdržel/a se", "not_voting": "přítomen/a, nehlasoval/a",
             "absent": "nepřítomen/a", "excused": "omluven/a"}
CAST = ("yes", "no", "abstain")
PROPOSER = {"government": "vládní návrh (předkládá člen vlády)", "mps": "poslanecký návrh",
            "senate": "návrh Senátu / senátorů", "region": "návrh krajského zastupitelstva", "other": "jiný dokument"}
SOURCE_LABEL = {"psp": "psp.cz", "senat": "senat.cz", "ep": "europarl.europa.eu"}


def term_label(chamber: str, term: str) -> str:
    if chamber == "psp":
        return {"2021": "2021–2025", "2025": "od 2025"}.get(term, f"od {term}")
    if chamber == "senat":
        return f"{term}. funkční období"
    return f"{term}. volební období"


@dataclass
class Term:
    chamber: str
    term: str
    events: list = field(default_factory=list)       # rows for the events JSON
    codes: dict = field(default_factory=dict)        # person -> list of option codes
    line: dict = field(default_factory=dict)         # person -> list of club-majority codes
    stats: dict = field(default_factory=dict)        # person -> dict
    dev_median: float | None = None

    @property
    def key(self) -> str:
        return f"{self.chamber}-{self.term}"


# ---------------------------------------------------------------- analysis

def club_lookup(conn, chamber: str):
    """Return f(person, date) -> club abbreviation (or None) for the chamber."""
    spans = defaultdict(list)
    for pid, start, end, abbr, name in conn.execute(
        "SELECT person_id, start_date, end_date, org_abbr, org_name FROM affiliation WHERE chamber=? AND kind=?",
        (chamber, CLUB_KIND[chamber]),
    ):
        spans[pid].append((start or "0000", end or "9999", abbr or name))
    for v in spans.values():
        v.sort()
    starts = {p: [s for s, _, _ in v] for p, v in spans.items()}
    cache = {}

    def at(pid, day):
        k = (pid, day)
        if k not in cache:
            res = None
            v = spans.get(pid)
            if v:
                i = bisect_right(starts[pid], day) - 1
                while i >= 0:
                    s, en, club = v[i]
                    if en >= day:
                        res = club
                        break
                    i -= 1
            cache[k] = res
        return cache[k]
    return at


def analyse_term(conn, chamber: str, term: str, people: set[int]) -> Term:
    """Vote list, attendance and agreement with the club majority for one chamber term."""
    t = Term(chamber, term)
    evs = conn.execute(
        "SELECT e.id, e.date, e.title, e.title_detail, e.outcome, e.url, b.number, b.url, e.yes_count, e.no_count,"
        " e.abstain_count, e.doc_ref, e.session, e.number FROM vote_event e LEFT JOIN bill b ON b.id = e.bill_id"
        " WHERE e.chamber=? AND e.term=? ORDER BY e.date, e.time, CAST(e.session AS INT), CAST(e.number AS INT)",
        (chamber, term),
    ).fetchall()
    idx = {r[0]: k for k, r in enumerate(evs)}
    dates = [r[1] for r in evs]
    void = {k for k, r in enumerate(evs) if r[4] == "void"}
    t.events = [[r[1], r[2] or "", r[3] or "", r[4] or "", r[5] or "", r[6] or r[11] or "", r[7] or "",
                 r[8], r[9], r[10], r[12] or "", r[13] or ""] for r in evs]
    club_at = club_lookup(conn, chamber)
    n = len(evs)
    votes = defaultdict(list)
    tally = defaultdict(Counter)
    for ev, pid, opt in conn.execute(
        "SELECT v.vote_event_id, v.person_id, v.option FROM vote v JOIN vote_event e ON e.id = v.vote_event_id"
        " WHERE e.chamber=? AND e.term=?", (chamber, term),
    ):
        k = idx[ev]
        club = club_at(pid, dates[k])
        if opt in CAST and club and k not in void:
            tally[(k, club)][opt] += 1
        if pid in people:
            votes[pid].append((k, opt, club))
    line = {}
    for (k, club), c in tally.items():
        total = sum(c.values())
        opt, cnt = c.most_common(1)[0]
        if total >= 3 and cnt * 2 > total:
            line[(k, club)] = opt
    for pid, vs in votes.items():
        codes, lcodes = ["."] * n, ["."] * n
        cnt = Counter()
        comparable = differ = 0
        for k, opt, club in vs:
            codes[k] = OPT_CODE.get(opt, "?")
            if k in void:
                continue
            cnt[opt] += 1
            lo = line.get((k, club))
            if lo:
                lcodes[k] = OPT_CODE[lo]
                if opt in CAST:
                    comparable += 1
                    differ += opt != lo
        rows = sum(cnt.values())
        t.codes[pid], t.line[pid] = "".join(codes), "".join(lcodes)
        t.stats[pid] = {
            "rows": rows, "counts": dict(cnt),
            "cast_rate": sum(cnt[o] for o in CAST) / rows if rows else None,
            "comparable": comparable, "differ": differ,
            "differ_rate": differ / comparable if comparable else None,
        }
    rates = [s["differ_rate"] for s in t.stats.values() if s["comparable"] >= 100]
    t.dev_median = statistics.median(rates) if rates else None
    return t


# ---------------------------------------------------------------- helpers

def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(s)).strip("-")


def person_name(r) -> str:
    return " ".join(x for x in (r["given_name"], r["family_name"]) if x) or f"#{r['id']}"


def full_name(r) -> str:
    parts = [r["title_before"], r["given_name"], r["family_name"]]
    s = " ".join(x for x in parts if x)
    return f"{s}, {r['title_after']}" if r["title_after"] else s


def fmt_date(d: str | None) -> str:
    if not d:
        return ""
    try:
        y, m, dd = d[:10].split("-")
        return f"{int(dd)}. {int(m)}. {y}"
    except ValueError:
        return d


def pct(x: float | None, digits: int = 1) -> str:
    return "–" if x is None else f"{100 * x:.{digits}f} %".replace(".", ",")


def thousands(n: int) -> str:
    return f"{n:,}".replace(",", "\u00a0")


def num(x: float, digits: int = 2) -> str:
    return f"{x:+.{digits}f}".replace(".", ",").replace("-", "−")


UPDATED: str | None = None  # date of the last ingest, set by build()
# psp.cz sometimes gives the same club a different abbreviation per term (ANO / ANO2011);
# the index uses the most recent abbreviation for each club name so filters don't split it.
CLUB_ABBR: dict[tuple[str, str], str] = {}


NAV = (("politici", "index.html", "Politici"), ("spektrum", "spektrum/index.html", "Spektrum"),
       ("metodika", "metodika.html", "Metodika"))


def page(title: str, body: str, depth: int, description: str = "", nav: str = "politici") -> str:
    up = "../" * depth
    updated = f"Stav dat k {fmt_date(UPDATED)}." if UPDATED else ""
    cur = ' aria-current="page"'
    links = "".join(f'<a href="{up}{href}"{cur if key == nav else ""}>{label}</a>' for key, href, label in NAV)
    return f"""<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(description)}">
<link rel="stylesheet" href="{up}static/style.css">
</head>
<body>
<header class="top"><div class="top-in"><a href="{up}index.html" class="brand"><span class="mark" aria-hidden="true">PW</span>Czech PoliWiki</a>
<nav>{links}</nav></div></header>
<main>
{body}
</main>
<footer><div class="foot-in"><p>Data z oficiálních zdrojů (psp.cz, senat.cz, data.europarl.europa.eu), bez hodnocení a pořadí. {updated}</p>
<p><a href="{up}metodika.html">Jak se čísla počítají</a> · <a href="https://github.com/DShaker/czech-poliwiki">Zdrojový kód</a></p></div></footer>
</body>
</html>
"""


# ---------------------------------------------------------------- spectrum charts

def strip_chart(points, highlight: int | None, link_prefix: str | None = None, names=None) -> str:
    """One row per club, members as dots along dimension 1, club median as a tick.

    points: list of (person_id, club, dim1, se1). Identity is carried by the row
    label, so no colour coding is needed; the highlighted member gets the accent.
    """
    rows = defaultdict(list)
    for pid, club, x, se in points:
        rows[club or "bez klubu"].append((pid, x, se))
    order = sorted(rows, key=lambda c: statistics.median(x for _, x, _ in rows[c]))
    lo = min(-3.0, min(x for _, _, x, _ in points) - 0.2)
    hi = max(3.0, max(x for _, _, x, _ in points) + 0.2)
    W, L, R, rh, top = 720, 150, 16, 26, 10
    H = top + rh * len(order) + 34

    def sx(x):
        return L + (x - lo) / (hi - lo) * (W - L - R)
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Rozložení na první dimenzi podle klubů">']
    for tick in range(int(lo), int(hi) + 1):
        out.append(f'<line class="grid{" zero" if tick == 0 else ""}" x1="{sx(tick):.1f}" x2="{sx(tick):.1f}" y1="{top}" y2="{H - 28}"/>')
        out.append(f'<text class="axis" x="{sx(tick):.1f}" y="{H - 14}" text-anchor="middle">{num(tick, 0) if tick else "0"}</text>')
    hl = None
    for i, club in enumerate(order):
        y = top + rh * i + rh / 2
        members = rows[club]
        out.append(f'<text class="lab" x="{L - 10}" y="{y + 4:.1f}" text-anchor="end">{e(club)} ({len(members)})</text>')
        med = statistics.median(x for _, x, _ in members)
        for pid, x, se in members:
            if pid == highlight:
                hl = (pid, x, se, y, club)
                continue
            tip = f"{names.get(pid, '')} ({club}): {num(x)}" if names else f"{club}: {num(x)}"
            dot = f'<circle class="dot" cx="{sx(x):.1f}" cy="{y:.1f}" r="4"><title>{e(tip)}</title></circle>'
            if link_prefix and names and pid in link_prefix:
                dot = f'<a href="{e(link_prefix[pid])}">{dot}</a>'
            out.append(dot)
        out.append(f'<line class="median" x1="{sx(med):.1f}" x2="{sx(med):.1f}" y1="{y - 9:.1f}" y2="{y + 9:.1f}"><title>medián {e(club)}: {num(med)}</title></line>')
    if hl:
        pid, x, se, y, club = hl
        out.append(f'<line class="ci" x1="{sx(x - 1.96 * se):.1f}" x2="{sx(x + 1.96 * se):.1f}" y1="{y:.1f}" y2="{y:.1f}"/>')
        out.append(f'<circle class="dot hl" cx="{sx(x):.1f}" cy="{y:.1f}" r="7"><title>{e(club)}: {num(x)} ± {1.96 * se:.2f}</title></circle>')
    out.append(f'<text class="axis" x="{W - R}" y="{H - 2}" text-anchor="end">dimenze 1 →</text></svg>')
    return "".join(out)


def scatter_chart(points, names, links) -> str:
    """Both dimensions; club medians labelled. points: (pid, club, d1, d2)."""
    W, H, pad = 720, 520, 36
    xs = [p[2] for p in points]
    ys = [p[3] for p in points]
    lo_x, hi_x = min(xs) - 0.3, max(xs) + 0.3
    lo_y, hi_y = min(ys) - 0.3, max(ys) + 0.3

    def sx(x):
        return pad + (x - lo_x) / (hi_x - lo_x) * (W - 2 * pad)

    def sy(y):
        return H - pad - (y - lo_y) / (hi_y - lo_y) * (H - 2 * pad)
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Rozložení ve dvou dimenzích">',
           f'<line class="grid zero" x1="{sx(0):.1f}" x2="{sx(0):.1f}" y1="{pad / 2}" y2="{H - pad}"/>',
           f'<line class="grid zero" x1="{pad}" x2="{W - pad / 2}" y1="{sy(0):.1f}" y2="{sy(0):.1f}"/>']
    clubs = defaultdict(list)
    for pid, club, x, y in points:
        clubs[club or "bez klubu"].append((x, y))
        dot = f'<circle class="dot" cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="4"><title>{e(names.get(pid, ""))} ({e(club or "bez klubu")}): {num(x)}, {num(y)}</title></circle>'
        if pid in links:
            dot = f'<a href="{e(links[pid])}">{dot}</a>'
        out.append(dot)
    for club, pts in clubs.items():
        if len(pts) < 2:
            continue
        mx = statistics.median(p[0] for p in pts)
        my = statistics.median(p[1] for p in pts)
        out.append(f'<text class="clublab" x="{sx(mx):.1f}" y="{sy(my) - 8:.1f}" text-anchor="middle">{e(club)}</text>')
        out.append(f'<rect class="medmark" x="{sx(mx) - 4:.1f}" y="{sy(my) - 4:.1f}" width="8" height="8"/>')
    out.append(f'<text class="axis" x="{W - pad}" y="{H - 8}" text-anchor="end">dimenze 1 →</text>')
    out.append(f'<text class="axis" x="10" y="{pad / 2 + 4}">↑ dimenze 2</text></svg>')
    return "".join(out)


# ---------------------------------------------------------------- build

def build(conn: sqlite3.Connection, out: str | Path) -> None:
    global UPDATED
    last = conn.execute("SELECT max(finished_at) FROM ingest_run").fetchone()[0]
    CLUB_ABBR.clear()
    for ch, name, abbr in conn.execute(
            "SELECT chamber, org_name, org_abbr FROM affiliation WHERE kind IN ('club','ep_group') AND org_abbr IS NOT NULL"
            " ORDER BY coalesce(start_date, '9999')"):
        CLUB_ABBR[(ch, name)] = abbr
    UPDATED = last[:10] if last else None
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    for d in ("p", "v", "spektrum", "static"):
        (out / d).mkdir(parents=True)
    static = resources.files("poliwiki").joinpath("static")
    for name in ("style.css", "votes.js", "search.js"):
        (out / "static" / name).write_text(static.joinpath(name).read_text("utf-8"), "utf-8")

    persons = conn.execute(
        "SELECT p.* FROM person p WHERE p.country='CZE' AND EXISTS (SELECT 1 FROM mandate m WHERE m.person_id=p.id)"
    ).fetchall()
    people = {r["id"]: r for r in persons}
    sources = defaultdict(dict)
    for src, sid, pid, url in conn.execute("SELECT source, source_id, person_id, url FROM person_source"):
        sources[pid][src] = (sid, url)
    slug = {}
    for pid, r in people.items():
        src = next((s for s in ("psp", "senat", "ep") if s in sources[pid]), None)
        slug[pid] = f"{slugify(person_name(r))}-{src}{sources[pid][src][0]}" if src else f"{slugify(person_name(r))}-{pid}"
    all_names = {pid: person_name(r) for pid, r in
                 ((r["id"], r) for r in conn.execute("SELECT * FROM person"))}

    terms = []
    for chamber, term in conn.execute("SELECT DISTINCT chamber, term FROM vote_event ORDER BY chamber, term").fetchall():
        t = analyse_term(conn, chamber, term, set(people))
        terms.append(t)
        (out / "v" / f"{t.key}.json").write_text(
            json.dumps({"chamber": chamber, "term": term, "events": t.events}, ensure_ascii=False, separators=(",", ":")),
            "utf-8")
        log.info("%s: %d events, %d profiled members", t.key, len(t.events), len(t.codes))

    has_ip = conn.execute("SELECT name FROM sqlite_master WHERE name='ideal_point'").fetchone()
    ideal = defaultdict(dict)       # (chamber, term) -> pid -> row
    fits = {}
    if has_ip:
        for r in conn.execute("SELECT * FROM ideal_point"):
            ideal[(r["chamber"], r["term"])][r["person_id"]] = r
        for r in conn.execute("SELECT * FROM spectrum_fit"):
            fits[(r["chamber"], r["term"])] = json.loads(r["detail"])
    term_clubs = {}
    for t in terms:
        end = t.events[-1][0] if t.events else "9999"
        club_at = club_lookup(conn, t.chamber)
        # club at the member's last vote in the term (or at the term's last vote)
        last_day = {}
        for pid, in conn.execute(
            "SELECT DISTINCT v.person_id FROM vote v JOIN vote_event e ON e.id=v.vote_event_id WHERE e.chamber=? AND e.term=?",
            (t.chamber, t.term)):
            last_day[pid] = end
        for pid, d in conn.execute(
            "SELECT v.person_id, max(e.date) FROM vote v JOIN vote_event e ON e.id=v.vote_event_id"
            " WHERE e.chamber=? AND e.term=? AND v.option IN ('yes','no','abstain') GROUP BY v.person_id",
            (t.chamber, t.term)):
            last_day[pid] = d
        term_clubs[(t.chamber, t.term)] = {pid: club_at(pid, d) for pid, d in last_day.items()}

    links_from_p = {pid: f"{slug[pid]}.html" for pid in people}
    links_from_s = {pid: f"../p/{slug[pid]}.html" for pid in people}

    index_rows = []
    for pid, r in people.items():
        html_body, meta = profile(conn, r, sources[pid], terms, ideal, term_clubs, fits, all_names, links_from_p)
        name = person_name(r)
        (out / "p" / f"{slug[pid]}.html").write_text(page(f"{name} · Czech PoliWiki", html_body, 1,
                                                         f"Hlasování, kluby a předložené návrhy: {name}"), "utf-8")
        (out / "p" / f"{slug[pid]}.json").write_text(json.dumps({
            "terms": [{"key": t.key, "label": f"{CHAMBER_SHORT[t.chamber]} {term_label(t.chamber, t.term)}",
                       "votes": t.codes[pid], "line": t.line[pid]} for t in terms if pid in t.codes]
        }, separators=(",", ":")), "utf-8")
        index_rows.append({"n": name, "f": r["family_name"] or "", "s": slug[pid], **meta})

    index_rows.sort(key=lambda x: (fold(x["f"]), fold(x["n"])))
    write_index(out, index_rows)
    write_spectrum_pages(out, terms, ideal, term_clubs, fits, all_names, links_from_s)
    (out / "metodika.html").write_text(page("Metodika · Czech PoliWiki", methodology(fits, terms), 0,
                                            "Jak se počítají údaje o hlasování a umístění na spektru", "metodika"), "utf-8")
    (out / ".nojekyll").write_text("")
    log.info("site: %d profiles written to %s", len(people), out)


def profile(conn, r, srcs, terms, ideal, term_clubs, fits, names, links):
    pid = r["id"]
    mandates = conn.execute(
        "SELECT * FROM mandate WHERE person_id=? ORDER BY start_date", (pid,)).fetchall()
    affs = conn.execute(
        "SELECT * FROM affiliation WHERE person_id=? ORDER BY chamber, kind, start_date", (pid,)).fetchall()
    bills = conn.execute(
        "SELECT b.* FROM bill b JOIN bill_sponsor s ON s.bill_id=b.id WHERE s.person_id=?"
        " ORDER BY b.submitted_date DESC, b.id", (pid,)).fetchall()
    chambers = []
    for m in mandates:
        if m["chamber"] not in chambers:
            chambers.append(m["chamber"])
    current = [m for m in mandates if not m["end_date"] or m["end_date"] >= datetime.now().date().isoformat()]
    cur_club = cur_label = None
    for a in affs:
        if a["kind"] in ("club", "ep_group") and not a["end_date"]:
            cur_club = a["org_abbr"] or a["org_name"]
            cur_label = CLUB_ABBR.get((a["chamber"], a["org_name"]), cur_club)

    b = [f'<h1>{e(full_name(r))}</h1>']
    facts = []
    if r["birth_date"]:
        facts.append(f"narozen/a {r['birth_date'][:4]}")
    facts.append(", ".join(f"{CHAMBER[c]}" for c in chambers))
    if cur_club:
        facts.append(e(cur_club))
    b.append(f'<p class="lede">{" · ".join(facts)}</p>')
    b.append('<p class="sources">Oficiální profil: ' + ", ".join(
        f'<a href="{e(u)}">{SOURCE_LABEL[s]}</a>' for s, (_, u) in srcs.items() if u) + "</p>")
    b.append('<nav class="toc"><a href="#mandaty">Mandáty a kluby</a><a href="#hlasovani">Hlasování</a>'
             '<a href="#spektrum">Spektrum</a><a href="#navrhy">Předložené návrhy</a><a href="#seznam">Všechna hlasování</a></nav>')

    # mandates and affiliations
    b.append('<section id="mandaty"><h2>Mandáty a kluby</h2><div class="tablewrap"><table><thead><tr><th>Orgán</th><th>Období</th><th>Od</th><th>Do</th><th>Obvod / kraj</th></tr></thead><tbody>')
    for m in mandates:
        b.append(f"<tr><td>{CHAMBER[m['chamber']]}</td><td>{e(term_label(m['chamber'], m['term']))}</td>"
                 f"<td>{fmt_date(m['start_date'])}</td><td>{fmt_date(m['end_date']) or 'dosud'}</td><td>{e(m['constituency'] or '')}</td></tr>")
    b.append("</tbody></table></div>")
    if affs:
        b.append('<div class="tablewrap"><table><thead><tr><th>Orgán</th><th>Druh</th><th>Název</th><th>Od</th><th>Do</th></tr></thead><tbody>')
        for a in affs:
            b.append(f"<tr><td>{CHAMBER_SHORT[a['chamber']]}</td><td>{AFF_LABEL.get(a['kind'], a['kind'])}</td>"
                     f"<td>{e(a['org_name'])}</td><td>{fmt_date(a['start_date'])}</td><td>{fmt_date(a['end_date']) or ('dosud' if a['start_date'] else '')}</td></tr>")
        b.append("</tbody></table></div>")
        if any(a["chamber"] == "senat" and a["kind"] == "club" and not a["start_date"] for a in affs):
            b.append('<p class="note">Senát zveřejňuje jen současný klub bez data vstupu.</p>')
    b.append("</section>")

    # voting summary
    b.append('<section id="hlasovani"><h2>Hlasování</h2>')
    mine = [t for t in terms if pid in t.stats]
    if not mine:
        b.append("<p>Žádná hlasování v datech.</p>")
    for t in mine:
        s = t.stats[pid]
        c = s["counts"]
        b.append(f'<h3>{CHAMBER[t.chamber]}, {e(term_label(t.chamber, t.term))}</h3>')
        b.append('<div class="tiles">')
        b.append(f'<div class="tile"><div class="big">{pct(s["cast_rate"])}</div><div>hlasoval/a pro, proti nebo se zdržel/a<br>'
                 f'<span class="muted">z {thousands(s["rows"])} hlasování během mandátu</span></div></div>')
        if s["comparable"]:
            med = f'medián {CHAMBER_SHORT[t.chamber]}: {pct(t.dev_median)}' if t.dev_median is not None else ""
            b.append(f'<div class="tile"><div class="big">{pct(s["differ_rate"])}</div><div>hlasoval/a jinak než většina vlastního klubu<br>'
                     f'<span class="muted">{s["differ"]} z {s["comparable"]} hlasování · {med}</span></div></div>')
        b.append("</div>")
        b.append('<div class="tablewrap"><table class="compact"><tbody>')
        for o in OPT_CODE:
            if c.get(o):
                b.append(f"<tr><td>{OPT_LABEL[o]}</td><td class='num'>{c[o]}</td><td class='num'>{pct(c[o] / s['rows'])}</td></tr>")
        b.append("</tbody></table></div>")
    b.append('<p class="note">Zmatečná hlasování nejsou započtena. Ve Sněmovně systém od roku 2013 nerozlišuje „zdržel se“ a „nestiskl tlačítko“. '
             '<a href="../metodika.html#ucast">Podrobnosti</a></p></section>')

    # spectrum
    b.append('<section id="spektrum"><h2>Umístění na spektru</h2>')
    any_ip = False
    for t in terms:
        row = ideal.get((t.chamber, t.term), {}).get(pid)
        if not row:
            continue
        any_ip = True
        clubs = term_clubs[(t.chamber, t.term)]
        pts = [(p, clubs.get(p), ip["dim1"], ip["se1"]) for p, ip in ideal[(t.chamber, t.term)].items()]
        if t.chamber == "ep":   # 700+ MEPs: keep the chart readable, Czech MEPs still highlighted via rows
            pass
        b.append(f'<h3>{CHAMBER[t.chamber]}, {e(term_label(t.chamber, t.term))}</h3>')
        b.append(f'<p>Dimenze 1: <strong>{num(row["dim1"])}</strong> (95% interval ± {1.96 * row["se1"]:.2f}), '
                 f'dimenze 2: {num(row["dim2"])}. Odhad z {row["n_votes"]} hlasování pro/proti. '
                 f'<a href="../spektrum/{t.key}.html">Celá komora</a></p>')
        b.append(strip_chart(pts, pid, links, names))
    if any_ip:
        b.append('<p class="note">Osy nemají předem daný význam ani název „levice/pravice“. Vznikají jen z toho, kdo s kým hlasuje stejně; '
                 'kladná strana je ta, kde leží největší klub. <a href="../metodika.html#spektrum">Jak se to počítá</a></p>')
    else:
        b.append("<p>Pro umístění je potřeba alespoň 20 hlasování pro/proti ve sporných otázkách.</p>")
    b.append("</section>")

    # bills
    b.append(f'<section id="navrhy"><h2>Předložené návrhy ({len(bills)})</h2>')
    if bills:
        b.append('<details open>' if len(bills) <= 10 else f'<details><summary>Zobrazit všech {len(bills)} návrhů</summary>')
        b.append('<div class="tablewrap"><table><thead><tr><th>Tisk</th><th>Název</th><th>Předloženo</th><th>Druh</th></tr></thead><tbody>')
        for bl in bills:
            b.append(f"<tr><td><a href='{e(bl['url'] or '')}'>{e(bl['number'] or '')}</a> <span class='muted'>{CHAMBER_SHORT[bl['chamber']]}</span></td>"
                     f"<td>{e(bl['title'] or '')}</td><td>{fmt_date(bl['submitted_date'])}</td><td>{PROPOSER.get(bl['proposer_kind'], '')}</td></tr>")
        b.append("</tbody></table></div></details>")
        b.append('<p class="note">U vládních návrhů je uveden člen vlády, který návrh předkládá.</p>')
    else:
        b.append("<p>V datech není žádný návrh, kde by byl/a uveden/a jako předkladatel.</p>")
    b.append("</section>")

    # vote list (client side)
    b.append(f'''<section id="seznam"><h2>Všechna hlasování</h2>
<div class="filters">
<input type="search" id="q" placeholder="Hledat v názvu, např. rozpočet" aria-label="Hledat">
<select id="term" aria-label="Období"></select>
<select id="opt" aria-label="Volba"><option value="">všechny volby</option><option value="y">pro</option><option value="n">proti</option><option value="a">zdržel/a se</option><option value="v">nehlasoval/a</option><option value="x">nepřítomen/a</option><option value="e">omluven/a</option></select>
<label><input type="checkbox" id="dev"> jen odlišně od klubu</label>
</div>
<p id="count" class="muted"></p>
<div class="tablewrap"><table id="votes"><thead><tr><th>Datum</th><th>Hlasování</th><th>Volba</th><th>Většina klubu</th><th>Výsledek</th></tr></thead><tbody></tbody></table></div>
<div class="pager"><button id="prev">Předchozí</button><span id="pageinfo"></span><button id="next">Další</button></div>
</section>
<script src="../static/votes.js" data-person="{e(links[pid].removesuffix(".html"))}.json"></script>''')

    last_club = None
    if not cur_club:
        ended = [a for a in affs if a["kind"] in ("club", "ep_group") and a["end_date"]]
        if ended:
            a = max(ended, key=lambda a: a["end_date"])
            last_club = CLUB_ABBR.get((a["chamber"], a["org_name"]), a["org_abbr"] or a["org_name"])
    meta = {"c": [CHAMBER_SHORT[m["chamber"]] + " " + term_label(m["chamber"], m["term"]) for m in mandates],
            "k": cur_label or "", "a": bool(current),
            "h": chambers, "hc": list(dict.fromkeys(m["chamber"] for m in current))}
    if last_club:
        meta["kl"] = last_club
    return "\n".join(b), meta


def write_index(out: Path, rows) -> None:
    now = Counter(c for r in rows if r["a"] for c in r["hc"])
    stats = "".join(f'<div class="stat"><span class="big">{now[c]}</span><span>{label}</span></div>'
                    for c, label in (("psp", "poslanců"), ("senat", "senátorů"), ("ep", "europoslanců")) if now[c])
    seg = "".join(f'<button type="button" data-ch="{c}">{label} <span class="n"></span></button>'
                  for c, label in (("", "Vše"), ("psp", "Sněmovna"), ("senat", "Senát"), ("ep", "Evropský parlament")))
    body = f"""<section class="hero"><h1>Politici</h1>
<p class="lede">Poslanci, senátoři a čeští europoslanci: jak hlasovali, v jakém klubu seděli a co předložili.
Bez hodnocení a pořadí, s odkazy na oficiální záznamy.</p>
<div class="stats">{stats}</div></section>
<section class="panel" aria-label="Filtry">
<div class="searchrow"><input type="search" id="q" placeholder="Hledat jméno nebo klub" aria-label="Hledat jméno nebo klub" autocomplete="off">
<label class="switch"><input type="checkbox" id="active" checked> <span>jen současní</span></label></div>
<div class="seg" id="chamber" role="group" aria-label="Komora">{seg}</div>
<div class="chips-wrap"><span class="chips-label">Klub / skupina</span><div class="chips" id="clubs" role="group" aria-label="Klub nebo skupina"></div></div>
</section>
<div class="listhead"><p id="count" class="muted" aria-live="polite"></p>
<label class="sortsel">Řadit <select id="sort" aria-label="Řadit podle">
<option value="name">podle příjmení</option><option value="club">podle klubu</option><option value="chamber">podle komory</option>
</select></label></div>
<div class="tablewrap card"><table id="people"><thead><tr>
<th aria-sort="ascending"><button type="button" data-sort="name">Jméno</button></th>
<th><button type="button" data-sort="chamber">Komora</button></th>
<th><button type="button" data-sort="club">Klub / skupina</button></th>
<th>Mandáty</th></tr></thead><tbody></tbody></table>
<p id="empty" class="empty" hidden>Nikdo neodpovídá filtrům. <button type="button" id="reset" class="linklike">Zrušit filtry</button></p></div>
<script id="data" type="application/json">{json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")}</script>
<script src="static/search.js"></script>"""
    (out / "index.html").write_text(page("Czech PoliWiki", body, 0, "Neutrální přehled hlasování českých politiků"), "utf-8")


def write_spectrum_pages(out: Path, terms, ideal, term_clubs, fits, names, links) -> None:
    items = []
    for t in terms:
        ip = ideal.get((t.chamber, t.term))
        if not ip:
            continue
        clubs = term_clubs[(t.chamber, t.term)]
        f = fits.get((t.chamber, t.term), {})
        title = f"{CHAMBER[t.chamber]}, {term_label(t.chamber, t.term)}"
        items.append(f'<li><a href="{t.key}.html">{e(title)}</a> <span class="muted">{len(ip)} členů</span></li>')
        pts1 = [(p, clubs.get(p), r["dim1"], r["se1"]) for p, r in ip.items()]
        pts2 = [(p, clubs.get(p), r["dim1"], r["dim2"]) for p, r in ip.items()]
        rows = sorted(ip.items(), key=lambda kv: kv[1]["dim1"])
        table = []
        for p, r in rows:
            nm = e(names.get(p, ""))
            if p in links:
                nm = f'<a href="{e(links[p])}">{nm}</a>'
            elif t.chamber == "ep":
                continue   # non-Czech MEPs: shown in the charts only
            table.append(f"<tr><td>{nm}</td><td>{e(clubs.get(p) or '')}</td><td class='num'>{num(r['dim1'])}</td>"
                         f"<td class='num'>± {1.96 * r['se1']:.2f}</td><td class='num'>{num(r['dim2'])}</td><td class='num'>{r['n_votes']}</td></tr>")
        c2 = f.get("classification_2d", {})
        c1 = f.get("classification_1d", {})
        body = f"""<h1>{e(title)}</h1>
<p class="lede">Umístění členů podle toho, jak hlasovali. Osy nemají předem daný význam; kladná strana je ta, kde leží největší klub
({e(f.get('orientation_club') or '–')}). <a href="../metodika.html#spektrum">Metodika</a></p>
<p>Model použil {f.get('votes_used', '?')} z {f.get('votes_total', '?')} hlasování a {f.get('members_used', '?')} z {f.get('members_total', '?')} členů.
Správně předpovídá {pct(c2.get('correct'))} hlasů pro/proti (jedna dimenze: {pct(c1.get('correct'))}), APRE {c2.get('apre', 0):.2f}.</p>
<h2>Dimenze 1 podle klubů</h2>
<p class="note">Tečka = člen, svislá čárka = medián klubu. Najetím zobrazíte jméno.</p>
{strip_chart(pts1, None, links, names)}
<h2>Obě dimenze</h2>
<p class="note">Čtvereček = medián klubu.</p>
{scatter_chart(pts2, names, links)}
<h2>Tabulka{' (čeští europoslanci)' if t.chamber == 'ep' else ''}</h2>
<div class="tablewrap"><table><thead><tr><th>Jméno</th><th>Klub</th><th>Dimenze 1</th><th>95% interval</th><th>Dimenze 2</th><th>Hlasování</th></tr></thead>
<tbody>{''.join(table)}</tbody></table></div>"""
        (out / "spektrum" / f"{t.key}.html").write_text(page(f"Spektrum: {title}", body, 1, nav="spektrum"), "utf-8")
    (out / "spektrum" / "index.html").write_text(page("Spektrum · Czech PoliWiki", f"""<h1>Spektrum</h1>
<p class="lede">Umístění členů každé komory zvlášť, odvozené jen z hlasování. Pozice z různých komor ani období nejsou
na stejné stupnici a nedají se porovnávat. <a href="../metodika.html#spektrum">Metodika</a></p>
<ul class="list">{''.join(items)}</ul>""", 1, nav="spektrum"), "utf-8")


def _se_pair(f) -> str:
    if "se_median_bootstrap" not in f:
        return "–"
    return f"{f['se_median_bootstrap']:.2f} / {f['se_median_analytic']:.2f}".replace(".", ",")


def methodology(fits, terms) -> str:
    rows = []
    for (ch, term), f in sorted(fits.items()):
        rows.append(f"<tr><td>{CHAMBER[ch]}, {e(term_label(ch, term))}</td><td class='num'>{f['members_used']}</td>"
                    f"<td class='num'>{f['votes_used']} / {f['votes_total']}</td><td class='num'>{pct(f['classification_1d']['correct'])}</td>"
                    f"<td class='num'>{pct(f['classification_2d']['correct'])}</td><td class='num'>{f['classification_2d']['apre']:.2f}</td>"
                    f"<td class='num'>{f['corr_dim1_pca']:.2f}</td>"
                    f"<td class='num'>{_se_pair(f)}</td></tr>")
    built = datetime.now(timezone.utc).strftime("%-d. %-m. %Y")
    text = resources.files("poliwiki").joinpath("static/metodika.html").read_text("utf-8")
    return text.replace("{{FIT_ROWS}}", "".join(rows)).replace("{{BUILT}}", built)
