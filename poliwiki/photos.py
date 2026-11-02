"""Photo credits that need the image itself.

psp.cz asks for "Foto: Kancelář Poslanecké sněmovny / <author>"; the author is
only recorded in each portrait's XMP metadata (dc:creator). This step downloads
the psp.cz portraits once (cached), reads the author and completes the credit.
The images themselves are not kept in the site: pages link to psp.cz.

    python -m poliwiki photos
"""
from __future__ import annotations

import logging
import re
import sqlite3
import urllib.error

from . import http
from .sources.psp import PHOTO_CREDIT

log = logging.getLogger(__name__)

_CREATOR = re.compile(rb"<dc:creator>\s*<rdf:Seq>(.*?)</rdf:Seq>", re.S)
_LI = re.compile(rb"<rdf:li[^>]*>(.*?)</rdf:li>", re.S)


def xmp_creator(jpeg: bytes) -> str | None:
    """Author(s) from a JPEG's XMP packet, joined with ', '."""
    m = _CREATOR.search(jpeg[:200_000])
    if not m:
        return None
    names = [n.decode("utf-8", "replace").strip() for n in _LI.findall(m.group(1))]
    return ", ".join(n for n in names if n) or None


def credit_psp(conn: sqlite3.Connection) -> int:
    rows = conn.execute("SELECT person_id, url FROM photo WHERE source='psp' AND credit=?", (PHOTO_CREDIT,)).fetchall()
    done = 0
    for pid, url in rows:
        try:
            body = http.get(url, retries=2, timeout=30)
        except http.NotFound:
            log.warning("psp photo missing: %s", url)
            conn.execute("DELETE FROM photo WHERE person_id=? AND source='psp'", (pid,))
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # psp.cz refuses most cloud IP ranges; keep the short credit and stop trying
            log.warning("psp.cz unreachable (%s); photo credits left without author", exc)
            break
        author = xmp_creator(body)
        if author:
            conn.execute("UPDATE photo SET credit=? WHERE person_id=? AND source='psp'",
                         (f"{PHOTO_CREDIT} / {author}", pid))
            done += 1
    conn.commit()
    log.info("photos: author added to %d of %d psp.cz credits", done, len(rows))
    return done
