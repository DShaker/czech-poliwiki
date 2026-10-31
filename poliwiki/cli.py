"""Command line entry point: python -m poliwiki <command>."""
from __future__ import annotations

import argparse
import logging

from . import db


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="poliwiki")
    p.add_argument("--db", default=str(db.DEFAULT_DB))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    ep = sub.add_parser("ep", help="European Parliament: MEPs and roll-call votes")
    ep.add_argument("--term", type=int, default=10)
    ep.add_argument("--since", help="only meetings on/after this ISO date")

    psp = sub.add_parser("psp", help="Poslanecká sněmovna from psp.cz open-data zips")
    psp.add_argument("--raw", default="data/raw/psp", help="directory with the downloaded zips")
    psp.add_argument("--term", default="2025", help="PSP term start year, e.g. 2021 or 2025")

    sen = sub.add_parser("senat", help="Senát: senators, clubs and votes")
    sen.add_argument("--psp-raw", default="data/raw/psp", help="psp.cz zips (identities, clubs)")
    sen.add_argument("--term", type=int, help="Senate term; default: the one senat.cz shows now")
    sen.add_argument("--cached", action="store_true", help="reuse cached RSS feeds")

    sub.add_parser("spectrum", help="fit ideal points for every chamber and term")
    site = sub.add_parser("site", help="build the static site")
    site.add_argument("--out", default="site")

    sub.add_parser("stats", help="row counts per table")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    conn = db.connect(a.db)
    if a.cmd == "ep":
        from .sources import ep as m
        m.ingest(conn, a.term, a.since)
    elif a.cmd == "psp":
        from .sources import psp as m
        m.ingest(conn, a.raw, a.term)
    elif a.cmd == "senat":
        from .sources import senat as m
        m.ingest(conn, a.psp_raw, a.term, refresh=not a.cached)
    elif a.cmd == "spectrum":
        from . import spectrum
        spectrum.run_all(conn)
    elif a.cmd == "site":
        from . import site
        site.build(conn, a.out)
    elif a.cmd == "stats":
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            print(f"{t:16} {conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0]:>10}")


if __name__ == "__main__":
    main()
