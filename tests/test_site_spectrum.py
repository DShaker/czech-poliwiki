import json

import numpy as np
import pytest

from poliwiki import db, site, spectrum


@pytest.fixture
def conn():
    return db.connect(":memory:")


def _chamber(conn, n_members=40, n_votes=150, seed=1):
    """Synthetic chamber: two clubs at -1 and +1 on one axis, logistic voting."""
    rng = np.random.default_rng(seed)
    x = np.concatenate([rng.normal(-1, 0.4, n_members // 2), rng.normal(1, 0.4, n_members - n_members // 2)])
    pids = []
    for i in range(n_members):
        pid = db.resolve_person(conn, "psp", str(i), given_name=f"P{i}", family_name="Test", country="CZE")
        club = "A" if i < n_members // 2 else "B"
        conn.execute("INSERT INTO mandate (person_id, chamber, term, start_date) VALUES (?, 'psp', 'T', '2020-01-01')", (pid,))
        conn.execute("INSERT INTO affiliation (person_id, chamber, kind, org_id, org_name, org_abbr, start_date)"
                     " VALUES (?, 'psp', 'club', ?, ?, ?, '2020-01-01')", (pid, club, club, club))
        pids.append(pid)
    for j in range(n_votes):
        b, a = rng.normal(0, 3), rng.normal(0, 1)
        ev = f"psp:T:{j}"
        conn.execute("INSERT INTO vote_event (id, chamber, term, date, number, title, outcome, url)"
                     " VALUES (?, 'psp', 'T', ?, ?, ?, 'passed', 'u')", (ev, f"2020-02-{1 + j % 28:02d}", str(j), f"vote {j}"))
        p = 1 / (1 + np.exp(-(a + b * x)))
        for pid, pr in zip(pids, p):
            conn.execute("INSERT INTO vote VALUES (?, ?, ?)", (ev, pid, "yes" if rng.random() < pr else "no"))
    conn.commit()
    return x, pids


def test_spectrum_recovers_synthetic_positions(conn):
    x, pids = _chamber(conn)
    detail = spectrum.run(conn, "psp", "T")
    est = dict(conn.execute("SELECT person_id, dim1 FROM ideal_point"))
    got = np.array([est[p] for p in pids])
    assert abs(np.corrcoef(got, x)[0, 1]) > 0.95
    assert detail["classification_2d"]["correct"] > 0.8
    assert all(conn.execute("SELECT se1 > 0 FROM ideal_point").fetchall())


def test_filter_drops_lopsided_votes_and_thin_members(monkeypatch):
    monkeypatch.setattr(spectrum, "MIN_VOTES", 2)
    Y = np.array([[1, 1, 0], [1, 0, 1], [1, 1, np.nan]] * 10, dtype=float)
    p, v, Yf = spectrum.filter_matrix(np.arange(30), np.array(["unanimous", "split", "split2"]), Y)
    assert list(v) == ["split", "split2"]
    assert len(p) == 20     # third row type keeps only one usable vote


def test_club_majority_and_deviation(conn):
    pids = [db.resolve_person(conn, "psp", str(i), given_name=f"P{i}", family_name="X", country="CZE") for i in range(4)]
    for pid in pids:
        conn.execute("INSERT INTO affiliation (person_id, chamber, kind, org_id, org_name, org_abbr, start_date, end_date)"
                     " VALUES (?, 'psp', 'club', 'A', 'Klub A', 'A', '2020-01-01', '2020-06-30')", (pid,))
    # member 3 moves to club B mid-year
    conn.execute("INSERT INTO affiliation (person_id, chamber, kind, org_id, org_name, org_abbr, start_date)"
                 " VALUES (?, 'psp', 'club', 'B', 'Klub B', 'B', '2020-07-01')", (pids[3],))
    rows = {
        ("e1", "2020-03-01"): ["yes", "yes", "yes", "no"],      # club A majority yes, member 3 differs
        ("e2", "2020-03-02"): ["yes", "no", "abstain", "yes"],  # no majority -> not counted
        ("e3", "2020-08-01"): ["no", "no", "no", "yes"],        # member 3 now in B (alone) -> no line
        ("e4", "2020-03-03"): ["yes", "yes", "yes", "absent"],
    }
    for (ev, day), opts in rows.items():
        conn.execute("INSERT INTO vote_event (id, chamber, term, date, number, outcome) VALUES (?, 'psp', 'T', ?, ?, 'passed')",
                     (ev, day, ev[1]))
        for pid, o in zip(pids, opts):
            conn.execute("INSERT INTO vote VALUES (?, ?, ?)", (ev, pid, o))
    t = site.analyse_term(conn, "psp", "T", set(pids))
    s3 = t.stats[pids[3]]
    assert (s3["comparable"], s3["differ"]) == (1, 1)
    assert s3["counts"]["absent"] == 1
    assert t.stats[pids[0]]["comparable"] == 2 and t.stats[pids[0]]["differ"] == 0
    # codes are ordered by date: e1, e2, e4, e3
    assert t.codes[pids[3]] == "nyxy"
    assert t.line[pids[3]] == "y.y."


def test_build_site(conn, tmp_path):
    _chamber(conn, n_members=24, n_votes=60)
    spectrum.run(conn, "psp", "T")
    site.build(conn, tmp_path / "out")
    out = tmp_path / "out"
    profiles = list((out / "p").glob("*.html"))
    assert len(profiles) == 24
    data = json.loads(next((out / "p").glob("*.json")).read_text())
    assert len(data["terms"][0]["votes"]) == 60
    assert (out / "spektrum" / "psp-T.html").exists()
    assert "{{" not in (out / "metodika.html").read_text()


def test_slugify():
    assert site.slugify("Vít Rakušan") == "vit-rakusan"


def test_bootstrap_errors_cover_truth_and_shrink_with_votes():
    se = {}
    for n_votes in (60, 240):
        c = db.connect(":memory:")
        x, pids = _chamber(c, n_votes=n_votes, seed=3)
        d = spectrum.run(c, "psp", "T")
        assert d["bootstrap"] == spectrum.BOOTSTRAP
        r = {p: (v, s) for p, v, s in c.execute("SELECT person_id, dim1, se1 FROM ideal_point")}
        est, err = np.array([r[p][0] for p in pids]), np.array([r[p][1] for p in pids])
        A = np.column_stack([np.ones_like(x), x])
        truth = A @ np.linalg.lstsq(A, est, rcond=None)[0]  # true positions on the estimate's scale
        assert np.mean(np.abs(est - truth) < 1.96 * err) > 0.85
        se[n_votes] = d["se_median_bootstrap"]
    assert se[240] < se[60]


def test_index_rows_carry_chambers_and_unified_club(conn, tmp_path):
    _chamber(conn, n_members=4, n_votes=5)
    # a former member whose club used an older abbreviation for the same club name
    pid = db.resolve_person(conn, "psp", "old", given_name="Old", family_name="Member", country="CZE")
    conn.execute("INSERT INTO mandate (person_id, chamber, term, start_date, end_date) VALUES (?, 'psp', 'S', '2016-01-01', '2019-12-31')", (pid,))
    conn.execute("INSERT INTO affiliation (person_id, chamber, kind, org_id, org_name, org_abbr, start_date, end_date)"
                 " VALUES (?, 'psp', 'club', 'A0', 'A', 'A-old', '2016-01-01', '2019-12-31')", (pid,))
    conn.commit()
    site.build(conn, tmp_path / "out")
    html = (tmp_path / "out" / "index.html").read_text()
    raw = html.split('<script id="data" type="application/json">')[1].split("</script>")[0]
    rows = {r["f"] + r["n"]: r for r in json.loads(raw.replace("<\\/", "</"))}
    old = rows["MemberOld Member"]
    assert old["a"] is False and old["h"] == ["psp"] and old["hc"] == []
    assert old["k"] == "" and old["kl"] == "A"     # unified to the newest abbreviation
    cur = next(r for r in rows.values() if r["a"])
    assert cur["hc"] == ["psp"] and cur["k"] in ("A", "B")
    assert 'data-ch="senat"' in html and 'aria-current="page"' in html


def test_event_rows_share_agendas_and_count_votes_per_item():
    # id, date, title, detail, outcome, url, print, print url, yes, no, abst, doc, session, number,
    # agenda_id, agenda, stage, motion, key_vote
    evs = [
        ("1", "2026-01-01", "Novela", None, "passed", "u", "7", "t", 1, 0, 0, None, "1", "1",
         "psp:bod:5", "Vládní návrh zákona /sněmovní tisk 7/", "3. čtení", None, None),
        ("2", "2026-01-01", "Novela", None, "passed", "u", "7", "t", 1, 0, 0, None, "1", "2",
         "psp:bod:5", "Vládní návrh zákona /sněmovní tisk 7/", "3. čtení", None, "závěrečné hlasování o návrhu (3. čtení)"),
        ("3", "2026-01-02", "Pořad schůze", None, "passed", "u", None, None, 1, 0, 0, None, "2", "1",
         None, None, None, None, None),
    ]
    rows, agendas = site.event_rows("psp", evs)
    assert agendas == [["Vládní návrh zákona /sněmovní tisk 7/", "3. čtení"]]
    assert [r[12] for r in rows] == [0, 0, -1]
    assert [(r[15], r[16]) for r in rows] == [(1, 2), (2, 2), (0, 0)]
    assert rows[1][14].startswith("závěrečné") and rows[2][17].startswith("Procedurální hlasování o pořadu")


def test_age_counts_whole_years():
    assert site.age("1954-09-02", "2026-09-01") == 71
    assert site.age("1954-09-02", "2026-09-02") == 72
    assert site.age("1954", "2026-01-01") is None and site.age(None, "2026-01-01") is None
    assert site.years(72) == "72 let" and site.years(3) == "3 roky" and site.years(1) == "1 rok"


def test_party_of_merges_spellings_only_from_the_list():
    from poliwiki.parties import party_of
    assert party_of("Poslanecký klub Občanské demokratické strany", "ODS")[0] == "ods"
    assert party_of("Občanská demokratická strana")[0] == "ods"
    assert party_of("Poslanecký klub ANO 2011", "ANO2011")[:2] == ("ano", "ANO")
    assert party_of("Nezařazení", "Nezařaz") is None
    assert party_of("bez politické příslušnosti") is None
    assert party_of("Strana Nová", None) == ("strana-nova", "Strana Nová", "Strana Nová")


def test_totals_count_law_proposals_per_proposer_and_lifetime_absence(conn):
    a, b, m = (db.resolve_person(conn, src, sid, given_name=n, family_name="X", country="CZE")
               for src, sid, n in (("psp", "1", "A"), ("psp", "2", "B"), ("ep", "3", "M")))
    for pid, ch in ((a, "psp"), (b, "psp"), (m, "ep")):
        conn.execute("INSERT INTO mandate (person_id, chamber, term, start_date) VALUES (?,?,'T','2020-01-01')", (pid, ch))
    bills = [("b1", "Návrh zákona", "mps", [a, b]), ("b2", "Vládní návrh zákona", "government", [a]),
             ("b3", "Mezinárodní smlouva", "government", [a]), ("b4", "Písemná interpelace", "mps", [b])]
    for bid, typ, kind, sp in bills:
        conn.execute("INSERT INTO bill (id, chamber, term, bill_type, proposer_kind) VALUES (?, 'psp', 'T', ?, ?)", (bid, typ, kind))
        for pid in sp:
            conn.execute("INSERT INTO bill_sponsor VALUES (?, ?, '')", (bid, pid))
    for i, opts in enumerate([("yes", "absent"), ("excused", "yes"), ("not_voting", "no"), ("yes", "yes")]):
        conn.execute("INSERT INTO vote_event (id, chamber, term, date, number, outcome) VALUES (?, 'psp', 'T', ?, ?, 'passed')",
                     (f"e{i}", f"2020-03-0{i + 1}", str(i)))
        for pid, o in zip((a, b), opts):
            conn.execute("INSERT INTO vote VALUES (?, ?, ?)", (f"e{i}", pid, o))
    t = site.analyse_term(conn, "psp", "T", {a, b, m})
    tot = site.person_totals(conn, [t], {a, b, m})
    assert (tot[a]["b"], tot[a]["bg"]) == (2, 1)       # co-sponsored bill counts in full, treaty doesn't
    assert tot[b]["b"] == 1                             # interpellation is not a law proposal
    assert "b" not in tot[m]                            # MEP: no such prints
    assert (tot[a]["ax"], tot[a]["ae"], tot[a]["ar"], tot[a]["ab"]) == (0, 1, 4, 0.25)
    assert tot[b]["ab"] == 0.25                         # not_voting is present, not absent


def test_xmp_creator_and_senate_photo():
    from poliwiki.photos import xmp_creator
    from poliwiki.sources.senat import parse_profile
    jpeg = b"\xff\xd8junk<dc:creator>\n <rdf:Seq>\n  <rdf:li>\xc5\xa0t\xc4\x9brba Martin</rdf:li>\n </rdf:Seq>\n</dc:creator>"
    assert xmp_creator(jpeg) == "Štěrba Martin"
    assert xmp_creator(b"\xff\xd8") is None
    page = '<img src="/images/logo-cs.png" /><img src="/images/senatori/fialat_295.jpg" alt="Tomáš Fiala" width="295" />'
    assert parse_profile(page)["photo"] == "/images/senatori/fialat_295.jpg"


def test_build_writes_party_pages_photos_and_age(conn, tmp_path):
    _chamber(conn, n_members=4, n_votes=5)
    pid = conn.execute("SELECT id FROM person ORDER BY id").fetchone()[0]
    conn.execute("UPDATE person SET birth_date='1970-01-01' WHERE id=?", (pid,))
    db.set_photo(conn, pid, "psp", "https://www.psp.cz/x/i0.jpg", "Foto: Kancelář Poslanecké sněmovny / Autor", "2021")
    db.set_photo(conn, pid, "psp", "https://www.psp.cz/old.jpg", "old", "2017")   # older mandate: ignored
    conn.commit()
    site.build(conn, tmp_path / "out")
    out = tmp_path / "out"
    assert (out / "strany" / "index.html").exists() and (out / "strany" / "a.html").exists()
    assert 'href="strany/index.html"' in (out / "index.html").read_text()
    prof = next(p for p in (out / "p").glob("*.html") if "i0.jpg" in p.read_text()).read_text()
    assert "Kancelář Poslanecké sněmovny / Autor" in prof and "old.jpg" not in prof
    assert " let</span>" in prof and "narozen/a 1970" not in prof
    assert 'href="../strany/' in prof
    other = [p.read_text() for p in (out / "p").glob("*.html") if "i0.jpg" not in p.read_text()]
    assert all('class="ph ph-none"' in h and "<img src" not in h for h in other)
    party = (out / "strany" / "a.html").read_text()
    assert "Současní členové" in party


def test_photo_credits_survive_single_timeouts(conn, monkeypatch):
    from poliwiki import http, photos
    from poliwiki.sources.psp import PHOTO_CREDIT
    for i in range(4):
        pid = db.resolve_person(conn, "psp", str(i), given_name=f"P{i}", family_name="X", country="CZE")
        db.set_photo(conn, pid, "psp", f"https://x/{i}.jpg", PHOTO_CREDIT, "2025")
    jpeg = b"<dc:creator><rdf:Seq><rdf:li>Autor</rdf:li></rdf:Seq></dc:creator>"

    def fake_get(url, **kw):
        if url.endswith("1.jpg"):
            raise TimeoutError("timed out")
        return jpeg
    monkeypatch.setattr(http, "get", fake_get)
    assert photos.credit_psp(conn) == 3
