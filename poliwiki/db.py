"""SQLite access and person identity resolution."""
from __future__ import annotations

import sqlite3
import unicodedata
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

DEFAULT_DB = Path("data/poliwiki.sqlite")


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL") if str(path) != ":memory:" else None
    conn.executescript(resources.files("poliwiki").joinpath("schema.sql").read_text("utf-8"))
    _migrate(conn)
    return conn


# columns added after the first release; CREATE TABLE IF NOT EXISTS leaves old databases without them
ADDED_COLUMNS = {"vote_event": ("agenda_id", "agenda", "stage", "motion", "key_vote")}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for c in cols:
            if c not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {c} TEXT")
    conn.commit()


def fold(s: str | None) -> str:
    """Lower-case, strip diacritics and whitespace for name matching."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def resolve_person(
    conn: sqlite3.Connection,
    source: str,
    source_id: str,
    *,
    given_name: str | None,
    family_name: str | None,
    birth_date: str | None = None,
    url: str | None = None,
    **attrs,
) -> int:
    """Return the person id for a source record, creating or linking as needed.

    Linking rule (deliberately conservative): a record from a new source is
    attached to an existing person only when folded given name, folded family
    name AND birth date all match exactly. Without a birth date no cross-source
    link is made, so two different people of the same name never merge.
    """
    row = conn.execute(
        "SELECT person_id FROM person_source WHERE source=? AND source_id=?",
        (source, str(source_id)),
    ).fetchone()
    if row:
        pid = row[0]
        _update_person(conn, pid, given_name=given_name, family_name=family_name,
                       birth_date=birth_date, **attrs)
        return pid

    pid = None
    if birth_date:
        for cand in conn.execute(
            "SELECT id, given_name, family_name FROM person WHERE birth_date=?",
            (birth_date,),
        ):
            if fold(cand["given_name"]) == fold(given_name) and fold(cand["family_name"]) == fold(family_name):
                # never link two records from the same source to one person
                clash = conn.execute(
                    "SELECT 1 FROM person_source WHERE person_id=? AND source=?",
                    (cand["id"], source),
                ).fetchone()
                if not clash:
                    pid = cand["id"]
                    break
    if pid is None:
        cols = {"given_name": given_name, "family_name": family_name, "birth_date": birth_date, **attrs}
        cur = conn.execute(
            f"INSERT INTO person ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(cols.values()),
        )
        pid = cur.lastrowid
    else:
        _update_person(conn, pid, **attrs)
    conn.execute(
        "INSERT INTO person_source (source, source_id, person_id, url) VALUES (?,?,?,?)",
        (source, str(source_id), pid, url),
    )
    return pid


def _update_person(conn, pid: int, **attrs) -> None:
    """Fill NULL columns only; never overwrite data another source supplied."""
    for col, val in attrs.items():
        if val is not None:
            conn.execute(f"UPDATE person SET {col}=? WHERE id=? AND {col} IS NULL", (val, pid))


PERSON_FK_TABLES = ("person_source", "mandate", "affiliation", "bill_sponsor", "vote")


def merge_persons(conn: sqlite3.Connection, keep: int, drop: int) -> None:
    """Move every row of person `drop` onto person `keep` and delete `drop`."""
    if keep == drop:
        return
    for table in PERSON_FK_TABLES:
        conn.execute(f"UPDATE OR IGNORE {table} SET person_id=? WHERE person_id=?", (keep, drop))
        conn.execute(f"DELETE FROM {table} WHERE person_id=?", (drop,))
    old = conn.execute("SELECT * FROM person WHERE id=?", (drop,)).fetchone()
    if old:
        _update_person(conn, keep, **{k: old[k] for k in old.keys() if k != "id"})
    conn.execute("DELETE FROM person WHERE id=?", (drop,))


class Run:
    """Context manager recording an ingest run."""

    def __init__(self, conn: sqlite3.Connection, source: str):
        self.conn, self.source, self.detail = conn, source, ""

    def __enter__(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.id = self.conn.execute(
            "INSERT INTO ingest_run (source, started_at) VALUES (?, ?)", (self.source, now)
        ).lastrowid
        return self

    def __exit__(self, exc_type, exc, tb):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        detail = self.detail if exc is None else f"FAILED: {exc!r}"
        self.conn.execute(
            "UPDATE ingest_run SET finished_at=?, detail=? WHERE id=?", (now, detail, self.id)
        )
        self.conn.commit()
        return False
