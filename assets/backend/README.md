# Backend data provider

For running without Google Cloud billing, see the free data provider (this
section). Hugging Face hosting (last section) now needs a paid PRO account.

The prepare step (`POST /api/v1/wildcat/fires/{fire_id}/prepare`) downloads the
3DEP 10 m DEM, the Landsat dNBR and the JRC water mask from one of two sources,
chosen with the `DATA_PROVIDER` environment variable:

| `DATA_PROVIDER` | Source |
|---|---|
| `auto` (default) | `gee` if `GEE_PROJECT_ID` is set, otherwise `planetary` |
| `gee` | Google Earth Engine (needs `GEE_PROJECT_ID` and Earth Engine credentials) |
| `planetary` | Microsoft Planetary Computer + USGS National Map, anonymous; no Google account or billing |

Both write the same files (same UTM grid, resolution, dtype and `-9999`
water/nodata value). Differences in `planetary` mode:

- The DEM comes straight from the USGS National Map's current 1/3 arc-second
  3DEP tiles. Earth Engine's 3DEP collection is a copy of the same tiles that
  is refreshed less often, so where USGS has published newer lidar the two
  DEMs differ (and so can Wildcat's basins).
- JRC Global Surface Water is v1.3 (1984–2020) instead of v1.4.
- MTBS perimeters (only used when a fire has no perimeter in `inputs/`) come
  from the USFS MTBS ArcGIS service.

`GET /api/v1/gee/status` checks whichever provider is active.

Notes:

- Existing deploys with `GEE_PROJECT_ID` set need no change: `auto` picks `gee`.
- To run without a Google account, leave `GEE_PROJECT_ID` unset. The Earth
  Engine sign-up steps in `instructions.html` are then optional.
- Planetary mode needs outbound HTTPS to `planetarycomputer.microsoft.com`,
  `*.blob.core.windows.net`, `prd-tnm.s3.amazonaws.com` and `apps.fs.usda.gov`.

# Hosting on a Hugging Face Space

> **Note (Nov 2026): on hold.** Hugging Face now requires a PRO subscription
> (about $9/month) for Docker Spaces; only static Spaces are free. Creating the
> Space fails with "402 Payment Required" on a free account. The steps below
> still work with PRO, but this is no longer a free option.

The same root `Dockerfile` runs on a Docker Space; nothing about the Cloud Run
deploy in `DEPLOYMENT_STATUS.md` changes. `deploy/huggingface/` holds the Space
card (`README.md`, with `sdk: docker` and `app_port: 8080`, the Dockerfile's
default port) and a script that uploads only the files the image needs.

**The Space is public, and so are ALL uploaded files:** the backend code, the
vendored `assets/wildcat` and `assets/asbpa_data`. Check that you are allowed to
publish them before running the script. A private Space can't be used, because
the website can't call it.

1. Create a free account at https://huggingface.co and a token with write
   access (Settings > Access Tokens).
2. `pip install -U huggingface_hub`, then `hf auth login`.
3. From the repo root: `python deploy/huggingface/deploy.py <hf-user>/<space-name>`
   (works on Windows too; creates the Space if needed and uploads). Only files committed
   to git are uploaded; `--dry-run` stages them without uploading. The first
   build takes several minutes.
4. In the Space, Settings > Variables and secrets, add:
   - `EXTRA_CORS_ORIGINS`: the exact origin of any frontend that isn't
     `https://subashpoudel1999.github.io`, e.g. `https://<user>.github.io`
     (comma-separated; `*` is ignored).
   - Nothing else is required. `DATA_PROVIDER` defaults to `auto`, which picks
     `planetary` while `GEE_PROJECT_ID` is unset, so no Google account is
     needed. To use Earth Engine instead, add `GEE_PROJECT_ID` and the
     credentials your Earth Engine setup needs as Space secrets (not
     covered here).
   - Optional: `FIRE_DATA_DIR=/data` if you attach persistent storage.
5. Check `https://<hf-user>-<space-name>.hf.space/health` and
   `/api/v1/gee/status` (should report `planetary`).
6. Point the site at it: in the GitHub repo, Settings > Secrets and variables >
   Actions > Variables, add `BACKEND_URL_OVERRIDE` =
   `https://<hf-user>-<space-name>.hf.space/api/v1`, then re-run the "Deploy
   Flutter web to GitHub Pages" workflow. With the variable unset the workflow
   uses the Cloud Run URL as before.

Limitations:

- Disk is ephemeral. Fire data and results under the data directory are lost
  whenever the Space restarts or rebuilds; treat it as a cache and re-run
  prepare/analyze. Job status is in memory on one process, as on Cloud Run.
- Free Spaces sleep after about 48 hours without traffic; the next request
  wakes in a minute or more. They can also be
  restarted at any time.
- The free CPU tier is 2 vCPU and 16 GB RAM (check HF's current docs). The
  prepare step holds one float32 stack of Landsat scenes per band (about
  150 MB for the Woolsey fire), well within that.
- The container runs as uid 1000; the Dockerfile makes `assets/wildcat`
  (the default data dir) writable for it.
