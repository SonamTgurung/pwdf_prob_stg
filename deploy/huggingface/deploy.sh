#!/usr/bin/env bash
# Upload the backend to a Hugging Face Docker Space.
# Usage: deploy/huggingface/deploy.sh <hf-user>/<space-name>
# Needs `pip install -U huggingface_hub` and `hf auth login` (write token).
set -euo pipefail
space="${1:?usage: $0 <hf-user>/<space-name>}"
root="$(cd "$(dirname "$0")/../.." && pwd)"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

# Same files the Dockerfile needs; mirrors .dockerignore.
mkdir -p "$stage/assets"
cp "$root/Dockerfile" "$stage/"
cp "$root/deploy/huggingface/README.md" "$stage/"
rsync -a --exclude __pycache__ --exclude .env --exclude .venv \
  "$root/assets/backend" "$root/assets/wildcat" "$stage/assets/"
rsync -a --exclude 'a7_vertical_land_motion/California_LLVS.txt' \
  --exclude 'a8_post_wildfire_debris_flow/combined_wildfire_debris_flow.shp' \
  --exclude 'a4_annual_high_tide_flood_days/processed_shoreline_flood_days.geojson' \
  "$root/assets/asbpa_data" "$stage/assets/"

hf repo create "$space" --repo-type space --space-sdk docker --exist-ok
hf upload "$space" "$stage" . --repo-type space
