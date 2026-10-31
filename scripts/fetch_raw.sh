#!/usr/bin/env bash
# Fetch raw source files into the directory given as $1.
# Failures are logged in MANIFEST.txt rather than aborting, so one broken
# source does not block the others.
set -u
OUT="${1:-out}"
mkdir -p "$OUT/psp" "$OUT/senat"
UA="czech-poliwiki/0.1 (+https://github.com/DShaker/czech-poliwiki)"
MANIFEST="$OUT/MANIFEST.txt"
: > "$MANIFEST"

get() {  # get URL DEST
  local code
  code=$(curl -sSL -A "$UA" --retry 3 --retry-delay 5 --max-time 600 \
    -o "$2" -w '%{http_code}' "$1" 2>>"$OUT/errors.log") || code="ERR"
  printf '%s\t%s\t%s\t%s\n' "$code" "$(stat -c %s "$2" 2>/dev/null || echo 0)" "$1" "$2" >> "$MANIFEST"
}

# Poslanecká sněmovna open data (https://www.psp.cz/sqw/hp.sqw?k=1300)
PSP=https://www.psp.cz/eknih/cdrom/opendata
for f in poslanci.zip hl-2025ps.zip hl-2021ps.zip tisky.zip schuze.zip; do
  get "$PSP/$f" "$OUT/psp/$f"
done

# Senát: probe pages (voting data has no bulk export)
get "https://www.senat.cz/senatori/index.php?lng=cz&O=15" "$OUT/senat/senatori.html"
get "https://www.senat.cz/xqw/xervlet/pssenat/hlas?O=15" "$OUT/senat/hlas.html"

date -u +%Y-%m-%dT%H:%M:%SZ > "$OUT/FETCHED_AT"
cat "$MANIFEST"
