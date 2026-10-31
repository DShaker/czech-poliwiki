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
