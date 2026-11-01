import zipfile

import pytest

from poliwiki import db
from poliwiki.sources import psp, senat


@pytest.fixture
def conn():
    return db.connect(":memory:")


def test_resolve_person_links_only_on_name_and_birth_date(conn):
    a = db.resolve_person(conn, "psp", "1", given_name="Kateřina", family_name="Konečná", birth_date="1981-03-18")
    b = db.resolve_person(conn, "ep", "23699", given_name="Katerina", family_name="KONECNA", birth_date="1981-03-18")
    c = db.resolve_person(conn, "ep", "99", given_name="Kateřina", family_name="Konečná")  # no birth date
    assert a == b
    assert c != a
    assert db.resolve_person(conn, "ep", "23699", given_name=None, family_name=None) == a


def test_resolve_person_never_merges_two_records_of_one_source(conn):
    a = db.resolve_person(conn, "psp", "1", given_name="Jan", family_name="Novák", birth_date="1970-01-01")
    b = db.resolve_person(conn, "psp", "2", given_name="Jan", family_name="Novák", birth_date="1970-01-01")
    assert a != b


def _unl(rows):
    return ("\n".join("|".join(r) + "|" for r in rows) + "\n").encode("cp1250")


@pytest.fixture
def raw(tmp_path):
    """Minimal psp.cz dump: one term, one club, two MPs, one bill, two votes."""
    with zipfile.ZipFile(tmp_path / "poslanci.zip", "w") as z:
        z.writestr("organy.unl", _unl([
            ["174", "", "11", "PSP10", "Poslanecká sněmovna", "Chamber", "04.10.2025", "", "", ""],
            ["1800", "174", "1", "ANO", "Poslanecký klub ANO", "", "05.10.2025", "", "", ""],
            ["99", "", "8", "PHA", "Hlavní město Praha", "", "", "", "", ""],
            ["500", "", "6", "ANO2011", "ANO 2011", "", "", "", "", ""],
        ]))
        z.writestr("osoby.unl", _unl([
            ["10", "Ing.", "Nováková", "Jana", "", "01.02.1980", "Z", "", ""],
            ["11", "", "Dvořák", "Petr", "Ph.D.", "03.04.1975", "M", "", ""],
        ]))
        z.writestr("poslanec.unl", _unl([
            ["900", "10", "99", "500", "174"] + [""] * 10,
            ["901", "11", "99", "500", "174"] + [""] * 10,
        ]))
        z.writestr("zarazeni.unl", _unl([
            ["10", "174", "0", "2025-10-04 00", "", "", ""],
            ["11", "174", "0", "2025-10-04 00", "", "", ""],
            ["10", "1800", "0", "2025-11-03 00", "", "", ""],
        ]))
    with zipfile.ZipFile(tmp_path / "tisky.zip", "w") as z:
        z.writestr("druh_tisku.unl", _unl([["1", "T", "Vládní návrh zákona"], ["2", "T", "Návrh zákona"]]))
        z.writestr("tisky.unl", _unl([
            ["5000", "2", "64", "7", "0", "3", "", "174", "", "Nováková J., Dvořák P.", "Novela",
             "15.10.2025", "", "", "", "Návrh poslanců na vydání zákona"],
        ]))
        z.writestr("predkladatel.unl", _unl([["5000", "11", "2", "0"], ["5000", "10", "1", "0"]]))
        z.writestr("typ_stavu.unl", _unl([["5", "3. čtení"]]))
        z.writestr("stavy.unl", _unl([["50", "5", "2", "", "", ""]]))
        z.writestr("typ_akce.unl", _unl([["22", "souhlas"]]))
        z.writestr("prechody.unl", _unl([["70", "50", "6", "22", "1"]]))
        # print history: vote 1 approved the bill in the 3rd reading, in agenda item 1
        z.writestr("hist.unl", _unl([["9", "5000", "2025-11-05 00", "1", "70", "1", "1"] + [""] * 8]))
    with zipfile.ZipFile(tmp_path / "schuze.zip", "w") as z:
        z.writestr("schuze.unl", _unl([["843", "174", "1", "2025-11-03 14", "", "", ""]]))
        # item 4 as changed during the sitting, and an unrelated item that had number 4 in the invitation
        z.writestr("bod_schuze.unl", _unl([
            ["2", "843", "", "0", "4", "Volba předsedy výboru", "", "", "1", "1"],
            ["1", "843", "5000", "5", "4", "Návrh poslanců na vydání zákona", "/sněmovní tisk 7/", "", "0", ""],
        ]))
    with zipfile.ZipFile(tmp_path / "hl-2025ps.zip", "w") as z:
        z.writestr("hl2025s.unl", _unl([
            ["1", "174", "1", "1", "4", "05.11.2025", "10:00", "1", "0", "0", "0", "2", "2", "N", "A", "Novela", ""],
            ["2", "174", "1", "2", "4", "05.11.2025", "11:00", "0", "1", "0", "0", "1", "1", "N", "R", "Novela**poslanců", ""],
            ["3", "174", "1", "3", "0", "05.11.2025", "12:00", "2", "0", "0", "0", "2", "2", "N", "A", "Pořad schůze", ""],
        ]))
        z.writestr("hl2025h1.unl", _unl([
            ["900", "1", "A"], ["901", "1", "K"], ["900", "2", "B"], ["901", "2", "@"],
        ]))
        z.writestr("omluvy.unl", _unl([["174", "901", "05.11.2025", "10:30", "12:00"]]))
        z.writestr("zmatecne.unl", _unl([]))
    return tmp_path


def test_psp_ingest(conn, raw):
    psp.ingest(conn, raw, "2025")
    people = {r["family_name"]: r for r in conn.execute("SELECT * FROM person")}
    assert people["Nováková"]["gender"] == "F" and people["Nováková"]["birth_date"] == "1980-02-01"
    votes = dict(conn.execute(
        "SELECT p.family_name || ':' || v.vote_event_id, v.option FROM vote v JOIN person p ON p.id=v.person_id"
    ).fetchall())
    assert votes == {
        "Nováková:psp:2025:1": "yes", "Dvořák:psp:2025:1": "abstain",
        "Nováková:psp:2025:2": "no", "Dvořák:psp:2025:2": "excused",  # '@' within an excuse window
    }
    ev = conn.execute("SELECT * FROM vote_event WHERE id='psp:2025:2'").fetchone()
    assert ev["outcome"] == "rejected" and ev["bill_id"] == "psp:2025:7" and ev["date"] == "2025-11-05"
    assert ev["title"] == "Novela poslanců"  # '**' is a line break in psp.cz short titles
    assert ev["agenda"] == "Návrh poslanců na vydání zákona /sněmovní tisk 7/" and ev["agenda_id"] == "psp:bod:1"
    assert ev["stage"] == "3. čtení" and ev["key_vote"] is None
    first = conn.execute("SELECT * FROM vote_event WHERE id='psp:2025:1'").fetchone()
    assert first["key_vote"] == "závěrečné hlasování o návrhu (3. čtení)"
    proc = conn.execute("SELECT * FROM vote_event WHERE id='psp:2025:3'").fetchone()
    assert proc["agenda"] is None and proc["bill_id"] is None
    bill = conn.execute("SELECT * FROM bill").fetchone()
    assert bill["proposer_kind"] == "mps"
    assert conn.execute("SELECT count(*) FROM bill_sponsor WHERE person_id IS NOT NULL").fetchone()[0] == 2
    club = conn.execute("SELECT * FROM affiliation WHERE kind='club'").fetchone()
    assert club["org_abbr"] == "ANO" and club["start_date"] == "2025-11-03"


FEED = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<item><guid isPermaLink="false">SENAT_PCR_H12149999</guid>
<title>15 - 31 - 21 - 300/15 - Návrh senátního návrhu zákona - o Policii</title>
<description>21 - přikázat výborům - pro; Senát odhlasoval: Návrh byl přijat</description>
<pubDate>Wed, 21 Oct 2026 15:30:21 GMT</pubDate></item>
<item><guid isPermaLink="false">SENAT_PCR_H12149990</guid>
<title>15 - 31 - 2 - 31. schůze Senátu</title>
<description>2 - schválit pořad - zdržela se; Senát odhlasoval: Návrh byl zamítnut</description>
<pubDate>Wed, 21 Oct 2026 22:30:00 GMT</pubDate></item>
</channel></rss>""".encode()


def test_senat_feed_parsing():
    a, b = list(senat.parse_feed(FEED))
    assert a["id"] == "12149999" and a["print"] == "300/15" and a["option"] == "yes"
    assert a["title"] == "Návrh senátního návrhu zákona - o Policii" and a["time"] == "17:30"
    assert b["print"] is None and b["option"] == "abstain" and b["outcome"] == "rejected"
    assert b["date"] == "2026-10-22"  # converted to Prague time


def test_senat_vote_event_describes_motion(conn):
    pid = db.resolve_person(conn, "senat", "1", given_name="A", family_name="B")
    items = list(senat.parse_feed(FEED))
    items[0]["motion"] = "vrátit PS"
    senat.ingest_votes(conn, "1", pid, 15, items)
    a = conn.execute("SELECT * FROM vote_event WHERE id='senat:15:31:21'").fetchone()
    assert a["motion"] == "vrátit Poslanecké sněmovně s pozměňovacími návrhy"
    assert a["key_vote"].startswith("závěrečné hlasování") and a["agenda"].endswith("(senátní tisk 300/15)")
    b = conn.execute("SELECT * FROM vote_event WHERE id='senat:15:31:2'").fetchone()
    assert b["key_vote"] is None and b["agenda"] is None and b["motion"] == "schválit pořad"
    assert senat.motion_label("3.pozměňovací návrh") == "3. pozměňovací návrh"


def test_proposer_kind():
    assert psp._proposer_kind("1", "1", "min. financí") == "government"
    assert psp._proposer_kind("3", "2", "Okamura T. a další") == "mps"
    assert psp._proposer_kind("4", "2", "Canov M. a další") == "senate"
    assert psp._proposer_kind("12", "2", "Netolický M.") == "region"
    assert psp._proposer_kind("0", "4", "ministr vnitra") == "government"
    assert psp._proposer_kind("0", "5", "ČNB") == "other"


def test_http_backs_off_on_429(monkeypatch, tmp_path):
    import io
    import urllib.error
    from poliwiki import http
    calls, sleeps = [], []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {"Retry-After": "7"}, None)
        return Resp(b"ok")

    monkeypatch.setattr(http.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(http.time, "sleep", sleeps.append)
    monkeypatch.setattr(http, "CACHE_DIR", tmp_path)
    assert http.get("https://example.org/x") == b"ok"
    assert len(calls) == 3 and sleeps.count(7.0) == 2


def test_agenda_item_choice_and_stage_labels():
    # two items with the same number in the changed agenda: the title decides
    a = ["1", "s", "", "0", "7", "Ústní interpelace", "", "", "1", ""]
    b = ["2", "s", "", "0", "7", "Návrh na potvrzení předsedy výboru", "", "", "0", ""]
    assert psp.pick_agenda_item([a, b], "Návrh na potvrzení předsedy výboru PS") is b
    assert psp.pick_agenda_item([], "x") is None
    assert psp.stage_label({"3. čtení", "2. čtení"}) == "2. a 3. čtení"
    assert psp.stage_label({"po vrácení Senátem"}) == "po vrácení Senátem"
    assert psp.reading("1. čtení (§ 90, odst. 2) - ob. rozprava") == "1. čtení"
    assert psp.key_vote_label("Vládní návrh zákona", "Sněmovna (Senát)", "setrváno") == "hlasování o zákonu, který vrátil Senát"


def test_ep_motion_and_procedure():
    from poliwiki.sources import ep
    assert ep.procedure("Monitorovací rámec pro lesy ***I") == ("Monitorovací rámec pro lesy", "řádný legislativní postup, 1. čtení")
    assert ep.procedure("Usnesení o Ukrajině") == ("Usnesení o Ukrajině", None)
    dec = {"headingLabel": {"cs": "Návrh na zamítnutí návrhu Komise", "en": "Proposal to reject"},
           "referenceText": {"cs": "návrh na zamítnutí", "en": "Proposal for rejection"},
           "responsible_organization_label": {"cs": "výbor"}}
    assert ep.motion(dec) == "Návrh na zamítnutí návrhu Komise · předložil: výbor"
    assert not ep.is_whole_text(dec)
    assert ep.is_whole_text({"referenceText": {"en": "Text as a whole", "cs": "celé znění"}})
    assert ep.is_whole_text({"referenceText": {"en": "Motion for a resolution (as a whole)"}})
    assert not ep.is_whole_text({"referenceText": {"en": "Motion for a resolution, § 4"}})
    assert not ep.is_whole_text({"referenceText": {"cs": "pn. 3"}}) and not ep.is_whole_text({})
    assert ep.motion({"referenceText": {"cs": "§ 9"}, "responsible_organization_label": {"cs": "původní znění"}}) == "§ 9 (původní znění)"
