# Backend data provider

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
