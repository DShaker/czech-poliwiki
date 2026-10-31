# Czech PoliWiki

Neutral aggregation of public records for Czech politicians: how each member of
the Poslanecká sněmovna, the Senát and the European Parliament voted, what they
sponsored, and which club or group they sat in. No scores or rankings: the
database stores what the official sources publish, with links back to them.

This repository is step 1 of 4: the data foundation. Next come per-politician
profile pages, an ideal-point (W-NOMINATE style) placement on the political
spectrum with a published method, and publishing on GitHub Pages.

## Quick start

```bash
pip install -e .[dev]
scripts/get_raw.sh                 # psp.cz dumps from the raw-data branch
python -m poliwiki psp --term 2025 # current Sněmovna term (2021 also works)
python -m poliwiki senat           # current Senát term
python -m poliwiki ep --term 10    # current European Parliament term
python -m poliwiki stats
pytest
```

Output: `data/poliwiki.sqlite`. Raw HTTP responses are cached in `data/cache`.

## Sources

| Chamber | What | Where from |
|---|---|---|
| Sněmovna | MPs, clubs, candidate lists, every roll-call vote with each MP's option, excuses, prints (bills) and their sponsors, link vote → bill | psp.cz open data dumps (`poslanci`, `hl-<term>ps`, `tisky`, `schuze`) |
| Senát | Senators' identity, birth date and club memberships | psp.cz `poslanci` dump (it also covers the Senate) |
| Senát | Party, constituency, mandate dates | senat.cz senator profile pages |
| Senát | Every vote each senator cast, with the motion and result | senat.cz official per-senator RSS feed `hlasovani_rss.php?pid=` |
| EP | MEPs, Czech MEPs' mandates, EP group and national party over time | data.europarl.europa.eu API v2 |
| EP | Every plenary roll-call vote, all MEPs (needed later to place Czech MEPs within the whole chamber) | data.europarl.europa.eu API v2 (`meetings/{id}/decisions`) |

psp.cz rejects connections from many cloud IP ranges, so the `fetch-raw`
workflow downloads its dumps daily from GitHub Actions and stores them on the
`raw-data` branch. senat.cz's vote database pages (`/xqw/xervlet/…`) sit behind
a firewall that rejects automated clients; the RSS feeds and profile pages are
used instead.

## Data model

`person` is one real person across all chambers. `person_source` maps each
source's own id to it. Records from two sources are linked only when given
name, family name and birth date all match (diacritics and case ignored), so
two different people with the same name are never merged. Senators that psp.cz
does not list yet get no birth date and therefore no cross-chamber link until
the next dump.

Other tables: `mandate` (seat periods), `affiliation` (club / party / EP group /
candidate list with dates), `bill` and `bill_sponsor`, `vote_event` and `vote`
(one row per person per vote), `ingest_run` (audit log).

Vote options are normalised to `yes`, `no`, `abstain`, `not_voting` (present,
did not vote), `absent`, `excused`.

## Caveats worth knowing

- **Sněmovna "K" code.** Since 2013 the chamber's voting system records
  "abstained or did not press" as one code; it is stored as `abstain`. In the
  Sněmovna an abstention counts the same as a no for passing a motion.
- **Sněmovna excuses.** An MP recorded as not logged in (`@`) is stored as
  `excused` when an excuse covers that time, otherwise `absent`.
- **Who proposed a print.** psp.cz does not publish the lookup table for
  `tisky.id_navrh`; the mapping (1 government, 2–3 MPs, 4–6 Senate, 7+ a
  regional assembly) was inferred from the data and is in `psp._proposer_kind`.
  For government bills the listed sponsors are the ministers who present them.
- **Senát totals.** The feeds give each senator's vote but not chamber totals,
  so `yes_count` etc. are empty for the Senate. Senators who left before the
  current list was published are not yet ingested.
- **EP absences.** The EP lists who voted and who attended each day. Attended
  without voting is `not_voting`, excused is `excused`; `absent` is filled only
  for Czech MEPs, whose mandate dates are known.
- **Void votes.** Votes declared void (`zmatecné`) are kept with outcome `void`;
  later analysis should drop them.
