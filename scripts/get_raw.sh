#!/usr/bin/env bash
# Pull the latest raw psp.cz snapshot (made by the fetch-raw workflow) into data/raw.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf data/raw && mkdir -p data
git clone -q --depth 1 -b raw-data "$(git remote get-url origin)" data/raw
rm -rf data/raw/.git
cat data/raw/FETCHED_AT
