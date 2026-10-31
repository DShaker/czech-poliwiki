# Czech PoliWiki

Neutral aggregation of public records for Czech politicians: how each member of
the Poslanecká sněmovna, the Senát and the European Parliament voted, what they
sponsored, and which club or group they sat in. No scores or rankings: the
database stores what the official sources publish, with links back to them.

Done so far: the data foundation (step 1), a static site with one profile per
politician (step 2) and an ideal-point placement on the political spectrum per
chamber and term with a published method (step 3), and a daily rebuild
published on GitHub Pages (step 4).

## Quick start

```bash
pip install -e .[dev]
scripts/get_raw.sh                 # psp.cz dumps from the raw-data branch
python -m poliwiki psp --term 2025 # current Sněmovna term (2021 also works)
python -m poliwiki senat           # current Senát term
python -m poliwiki ep --term 10    # current European Parliament term
python -m poliwiki stats
python -m poliwiki spectrum        # ideal points per chamber and term
python -m poliwiki site --out site # static site
python -m http.server -d site      # preview at http://localhost:8000
pytest
```

Output: `data/poliwiki.sqlite`. Raw HTTP responses are cached in `data/cache`.

## The site

`poliwiki site` writes plain HTML, CSS and JS (no build tools) to `site/`:

- `index.html`: every Czech MP, senator and MEP with a mandate in the data, searchable.
- `p/<name>-<source id>.html`: profile with mandates, clubs over time, attendance,
  share of votes against the own club's majority (with the chamber median for
  context), spectrum position, sponsored prints and a searchable list of every
  vote linking to the official record. The vote list is loaded from
  `p/<…>.json` (one character per vote) plus a shared `v/<chamber>-<term>.json`.
- `spektrum/`: per chamber and term, all members on both dimensions.
- `metodika.html`: the method, in Czech, with the fit statistics of the current run.

The UI is in Czech because the audience is Czech voters.

## Publishing and refresh

Two workflows keep the site current:

1. `fetch-raw` (daily, 03:17 UTC) downloads the psp.cz dumps to the `raw-data`
   branch, because psp.cz rejects most cloud IP ranges but accepts GitHub Actions.
2. `publish` runs after every successful `fetch-raw`, on pushes to `main` that
   touch the code, or by hand. It rebuilds the database from scratch (Sněmovna
   2021 and 2025, Senát, EP term 10), fits the spectrum, builds `site/` and
   deploys it to GitHub Pages. If any step fails, the previous site stays up.

The HTTP cache (`data/cache`) is kept between runs with `actions/cache`, so
only new EP meetings and the senators' feeds are downloaded again. The cache
key changes every month, which forces a full refetch at least that often.

One-off repository setup: Settings → Pages → Source: **GitHub Actions**.
Scheduled and `workflow_run` triggers only fire from the default branch (`main`).

## Spectrum model

`poliwiki/spectrum.py` fits a two-dimensional logistic item-response
ideal-point model (Clinton, Jackman & Rivers 2004; same family as W-NOMINATE)
to yes/no votes, separately for each chamber and term. Abstentions and absences
are missing data; void and near-unanimous votes (minority under 2.5 %) are
dropped; members need 20 usable votes. Dimension 1 is aligned with a
one-dimensional fit, dimension 2 is the orthogonal remainder, and the sign puts
the largest club on the positive side. The axes carry no left/right label.
Standard errors come from a block bootstrap over sitting days (50 resamples of
whole days, each refitted and mapped onto the main fit's scale by least squares;
votes on one day are not independent), which includes the
uncertainty in the vote parameters that the analytic errors leave out.
Results land in tables `ideal_point` and `spectrum_fit`.

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
  so `yes_count` etc. are empty for the Senate. Senators who left mid-term are
  found by sampling senat.cz's list "as of" a date every 14 days; each
  senator's profile is read as of their last vote in the term, so the mandate
  and club shown are the ones they held then.
- **EP absences.** The EP lists who voted and who attended each day. Attended
  without voting is `not_voting`, excused is `excused`; `absent` is filled only
  for Czech MEPs, whose mandate dates are known.
- **Void votes.** Votes declared void (`zmatecné`) are kept with outcome `void`;
  later analysis should drop them.
