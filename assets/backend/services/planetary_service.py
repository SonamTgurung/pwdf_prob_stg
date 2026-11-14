"""
Microsoft Planetary Computer data provider — a no-account, no-billing
alternative to gee_service / the GEE parts of wildcat_fire_prep_service.

Same inputs, same outputs (file layout, UTM grid, resolution, dtype and the
WATER_SENTINEL nodata) as the Earth Engine path, so Wildcat / MHRI don't care
which provider produced the files. Selected with DATA_PROVIDER (see
wildcat_fire_prep_service.get_data_provider).

Data (anonymous access; Planetary Computer assets are signed via the
`planetary-computer` package):
  - DEM:   USGS National Map 3DEP 1/3 arc-second "current" 1x1 degree COG
           tiles on public S3 (prd-tnm.s3.amazonaws.com) — the tiles GEE's
           USGS/3DEP/10m_collection is ingested from (same names and
           geotransforms). USGS updates them as new lidar arrives, so they
           can be newer than GEE's copy. (Planetary Computer's
           `3dep-seamless` stops at a ~2020 snapshot, so it isn't used.)
  - dNBR:  STAC `landsat-c2-l2`, Landsat 8/9 T1 (Landsat 7 fallback),
           assets nir08 (SR_B5 / L7 SR_B4) and swir22 (SR_B7)
  - Water: STAC `jrc-gsw`, asset `occurrence` (PC hosts GSW v1.3, 1984-2020;
           the GEE path uses v1.4, 1984-2021)
  - MTBS perimeters: USFS EDW MTBS ArcGIS REST service (no auth)

Usage:
    import services.planetary_service as pc_svc
    pc_svc.download_dem_clip(perimeter_gdf, buffer_m=3000, out_path="/tmp/dem.tif")
"""

import logging
import math
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests
from rasterio.transform import Affine
from rasterio.warp import Resampling, reproject, transform_bounds

log = logging.getLogger(__name__)

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
# USGS 3DEP 1/3 arc-second DEM tiles, anonymous HTTPS. `name` is the tile's
# north-west corner, e.g. n38w123 covers 37-38 N, 123-122 W.
TNM_DEM_URL = (
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/"
    "{name}/USGS_13_{name}.tif"
)
# MTBS burned-area boundaries layer of the USFS EDW MTBS service. The layer
# index (63) is set by USFS and may change when they republish the service.
MTBS_QUERY_URL = (
    "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_MTBS_01/MapServer/63/query"
)

# Same value as gee_service.WATER_SENTINEL (not imported so this module
# doesn't need `import ee`).
WATER_SENTINEL = -9999.0

# PC's STAC footprints can be slightly smaller than the real pixel extent
# (e.g. LC08_042036_20181126 has an edge pixel in the Malibu AOI that its
# footprint misses, while GEE's filterBounds keeps the scene). Searching a
# padded bbox and letting nodata drop out per pixel gives the same median.
_LANDSAT_SEARCH_PAD_DEG = 0.1

# GDAL settings for reading remote COGs.
_GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_TIMEOUT": "60",
    "GDAL_HTTP_CONNECTTIMEOUT": "20",
}

# (connect, read) timeout in seconds for STAC API requests.
_STAC_TIMEOUT = (20, 60)


def _catalog():
    # Asset hrefs are signed right before they're read (_read_to_grid), so a
    # long prep can't outlive a token signed at search time.
    import pystac_client
    return pystac_client.Client.open(STAC_URL, timeout=_STAC_TIMEOUT)


def test_connection() -> dict:
    """Check the Planetary Computer STAC API (Landsat) and the USGS 3DEP
    tiles are reachable. Same response shape as gee_service.test_connection()."""
    try:
        _catalog().get_collection("landsat-c2-l2")
        if _tnm_tile_exists("n38w123"):  # any known land tile will do
            return {
                "success": True,
                "message": "Connected to Microsoft Planetary Computer and USGS TNM. "
                           "3DEP 10m DEM is accessible.",
                "provider": "planetary",
            }
        return {"success": False, "message": "Connected but could not verify 3DEP dataset.",
                "provider": "planetary"}
    except Exception as e:  # noqa: BLE001 - report any failure as a status
        return {"success": False, "message": f"Planetary Computer connection failed: {e!s}",
                "provider": "planetary"}


# ── Target grid (mirrors GEE getDownloadURL's scale/crs/region behaviour) ─────

def _snap_grid(bounds: tuple, scale: int, utm_epsg: int) -> dict:
    """Pixel grid GEE exports for `region` bounds at `scale` in `crs`: pixel
    edges on multiples of `scale`, covering every pixel the bounds touch."""
    minx, miny, maxx, maxy = bounds
    x0 = math.floor(minx / scale) * scale
    y0 = math.floor(miny / scale) * scale
    x1 = math.ceil(maxx / scale) * scale
    y1 = math.ceil(maxy / scale) * scale
    return {
        "crs": f"EPSG:{utm_epsg}",
        "transform": Affine(scale, 0.0, x0, 0.0, -scale, y1),
        "width": round((x1 - x0) / scale),
        "height": round((y1 - y0) / scale),
    }


def _grid_bbox_wgs84(grid: dict) -> list:
    t = grid["transform"]
    minx, maxy = t.c, t.f
    maxx, miny = minx + grid["width"] * t.a, maxy + grid["height"] * t.e
    return list(transform_bounds(grid["crs"], "EPSG:4326", minx, miny, maxx, maxy, densify_pts=21))


def _read_to_grid(href: str, grid: dict, src_nodata=None, dtype="float32", fill=np.nan) -> np.ndarray:
    """Read band 1 of a (remote) raster resampled onto `grid` with nearest
    neighbour — GEE's default resampling for exports."""
    import planetary_computer
    dst = np.full((grid["height"], grid["width"]), fill, dtype=dtype)
    # SAS token for PC blob storage, cached per storage account; any other
    # URL (e.g. the USGS DEM tiles) is returned unchanged.
    href = planetary_computer.sign(href)
    with rasterio.Env(**_GDAL_ENV), rasterio.open(href) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_nodata=src_nodata if src_nodata is not None else src.nodata,
            dst_transform=grid["transform"],
            dst_crs=grid["crs"],
            dst_nodata=fill,
            resampling=Resampling.nearest,
            init_dest_nodata=False,
            tolerance=0,  # exact transformer, no approximation of pixel centres
        )
    return dst


def _write_grid(arr: np.ndarray, grid: dict, out_path: str) -> str:
    """Write a single-band GeoTIFF laid out like GEE's GEO_TIFF downloads
    (tiled 256x256, deflate) tagged with WATER_SENTINEL as NoData."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        out_path, "w", driver="GTiff", count=1, dtype=arr.dtype.name,
        width=grid["width"], height=grid["height"], crs=grid["crs"],
        transform=grid["transform"], nodata=WATER_SENTINEL,
        tiled=True, blockxsize=256, blockysize=256, compress="deflate",
    ) as dst:
        dst.write(arr, 1)
    return out_path


def water_mask(grid: dict) -> np.ndarray:
    """Boolean water mask on `grid` from JRC GSW occurrence — same rule as
    gee_service.land_mask_image(): water = occurrence >= 50, anything
    without data counts as land (GEE's unmask(0))."""
    items = _catalog().search(collections=["jrc-gsw"], bbox=_grid_bbox_wgs84(grid)).items()
    water = np.zeros((grid["height"], grid["width"]), dtype=bool)
    for item in items:
        occ = _read_to_grid(item.assets["occurrence"].href, grid, dtype="uint8", fill=0)
        water |= (occ >= 50) & (occ <= 100)  # 255 = no data in the raw GSW tiles
    return water


# ── DEM ───────────────────────────────────────────────────────────────────────

def _tnm_tile_names(bbox: list) -> list:
    """Names of the 1x1 degree USGS tiles whose degree cell intersects a
    lon/lat bbox, sorted. Tiles also carry a 6-pixel (0.00056 deg) overlap
    buffer on every side, so these cells always cover the bbox fully."""
    minx, miny, maxx, maxy = bbox
    names = []
    for top in range(math.floor(miny) + 1, math.ceil(maxy) + 1):
        for left in range(math.floor(minx), math.ceil(maxx)):
            names.append(f"{'n' if top > 0 else 's'}{abs(top):02d}"
                         f"{'w' if left < 0 else 'e'}{abs(left):03d}")
    return sorted(names)


def _tnm_tile_exists(name: str) -> bool:
    """HEAD the tile; 404 means USGS has no tile there (open ocean). Any
    other failure is raised rather than treated as a missing tile."""
    from urllib3.util.retry import Retry
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=Retry(
        total=5, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=None)))
    resp = session.head(TNM_DEM_URL.format(name=name), timeout=_STAC_TIMEOUT)
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return True


def download_dem_clip(
    perimeter_gdf: gpd.GeoDataFrame,
    buffer_m: int,
    out_path: str,
    mask_water: bool = True,
) -> str:
    """
    10m USGS 3DEP DEM clipped to perimeter + buffer_m metres, read from the
    USGS National Map tiles (TNM_DEM_URL) — the planetary-mode equivalent
    of gee_service.download_dem_clip().

    Output: float32 GeoTIFF in the fire's UTM zone at 10 m, water and
    no-data pixels set to WATER_SENTINEL (tagged as NoData).
    """
    centroid_lon = perimeter_gdf.to_crs(epsg=4326).geometry.centroid.iloc[0].x
    utm_epsg = _utm_epsg_from_lon(centroid_lon)
    buffered_utm = perimeter_gdf.to_crs(epsg=utm_epsg).buffer(buffer_m)
    bounds_utm = tuple(float(v) for v in buffered_utm.total_bounds)
    grid = _snap_grid(bounds_utm, 10, utm_epsg)

    # The tiles are NAD83 lon/lat; NAD83 -> WGS84 is a null shift in PROJ.
    names = [n for n in _tnm_tile_names(_grid_bbox_wgs84(grid)) if _tnm_tile_exists(n)]
    if not names:
        raise RuntimeError("No USGS 3DEP 1/3 arc-second DEM tiles cover this area.")
    log.info("3DEP 1/3 arc-second tiles: %s", names)

    # Where tile buffers overlap, the first tile in name order wins (in the
    # western US: the southern tile, then the eastern one). That matched
    # GEE's mosaic() in the one seam we could check (CZU: n37w123 over
    # n38w123); GEE doesn't document its order. Neighbouring tiles agree in
    # the buffer apart from small seam differences.
    dem = np.full((grid["height"], grid["width"]), np.nan, dtype="float32")
    for name in names:
        tile = _read_to_grid(TNM_DEM_URL.format(name=name), grid)
        fill = np.isnan(dem)
        dem[fill] = tile[fill]

    missing = np.isnan(dem)
    if mask_water:
        missing |= water_mask(grid)
    dem[missing] = WATER_SENTINEL
    return _write_grid(dem, grid, out_path)


# ── dNBR ──────────────────────────────────────────────────────────────────────

def _landsat_items(cat, bbox: list, start: str, end: str, max_cloud: float, platforms: list) -> list:
    """Landsat C2 L2 Tier-1 scenes intersecting bbox with start <= date < end
    (GEE filterDate is end-exclusive) and scene CLOUD_COVER < max_cloud."""
    pad = _LANDSAT_SEARCH_PAD_DEG
    search = cat.search(
        collections=["landsat-c2-l2"],
        bbox=[bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad],
        datetime=f"{start}T00:00:00Z/{end}T00:00:00Z",
        query={
            "platform": {"in": platforms},
            "landsat:collection_category": {"eq": "T1"},
            "eo:cloud_cover": {"lt": max_cloud},
        },
    )
    end_key = end.replace("-", "")
    return [i for i in search.items() if i.datetime.strftime("%Y%m%d") < end_key]


def _covering(items: list, bbox: list) -> list:
    """Items whose STAC footprint intersects bbox (the unpadded output area)."""
    from shapely.geometry import box, shape
    area = box(*bbox)
    return [i for i in items if shape(i.geometry).intersects(area)]


def _median_dn(items: list, grid: dict, band: str) -> np.ndarray:
    """Per-pixel median of a band's raw DN across scenes (nodata 0 ignored).
    Median commutes with the linear SR scale, so scaling afterwards gives the
    same values as GEE's scale-then-median while keeping memory low."""
    h, w = grid["height"], grid["width"]
    if not items:
        return np.full((h, w), np.nan)
    stack = np.empty((len(items), h, w), dtype="float32")

    def read(k):
        stack[k] = _read_to_grid(items[k].assets[band].href, grid, src_nodata=0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(read, range(len(items))))
    out = np.empty((h, w), dtype="float64")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN pixels stay NaN
        for r in range(0, h, 128):  # row chunks cap nanmedian's working copy
            out[r:r + 128] = np.nanmedian(stack[:, r:r + 128], axis=0)
    return out


def _nbr_of_median(items: list, grid: dict) -> np.ndarray:
    nir = _median_dn(items, grid, "nir08") * 0.0000275 - 0.2   # SR_B5 (L7: SR_B4)
    swir = _median_dn(items, grid, "swir22") * 0.0000275 - 0.2  # SR_B7
    with np.errstate(divide="ignore", invalid="ignore"):
        nbr = (nir - swir) / (nir + swir)
    nbr[~np.isfinite(nbr)] = np.nan  # GEE masks x/0
    return nbr


def download_dnbr(
    perim_gdf: gpd.GeoDataFrame,
    fire_date: str,
    out_path: str,
    buffer_m: int = 3000,
    scale: int = 30,
) -> None:
    """
    Landsat dNBR — the Planetary Computer equivalent of
    wildcat_fire_prep_service._download_dnbr(), same method:

    Pre-fire window:  fire_date − 365 days  →  fire_date − 14 days  (cloud < 20)
    Post-fire window: fire_date + 14 days   →  fire_date + 180 days (cloud < 30)

    Per-band median composites of scaled SR (×0.0000275 − 0.2), NBR of the
    medians, dNBR = (pre − post) × 1000, water → WATER_SENTINEL. No per-pixel
    QA/cloud masking (the GEE path doesn't do any either). Falls back to
    Landsat-7 if L8/L9 have no scenes.
    """
    from services.wildcat_fire_prep_service import _parse_fire_date

    # Buffered ROI in WGS84, exported over its UTM footprint (like GEE)
    centroid_lon = perim_gdf.to_crs(epsg=4326).geometry.centroid.iloc[0].x
    utm_epsg = _utm_epsg_from_lon(centroid_lon)
    buf_utm = perim_gdf.to_crs(epsg=utm_epsg).buffer(buffer_m)
    b = [float(v) for v in buf_utm.to_crs(epsg=4326).total_bounds]
    roi_utm = transform_bounds("EPSG:4326", f"EPSG:{utm_epsg}", *b, densify_pts=1000)
    grid = _snap_grid(roi_utm, scale, utm_epsg)

    fire_dt = _parse_fire_date(fire_date)
    pre_start = (fire_dt - timedelta(days=365)).strftime("%Y-%m-%d")
    pre_end = (fire_dt - timedelta(days=14)).strftime("%Y-%m-%d")
    post_start = (fire_dt + timedelta(days=14)).strftime("%Y-%m-%d")
    post_end = (fire_dt + timedelta(days=180)).strftime("%Y-%m-%d")

    # The median uses every scene from the padded search (see
    # _LANDSAT_SEARCH_PAD_DEG), but whether there is any coverage at all —
    # the L7 fallback and the no-scenes error — is decided only on scenes
    # whose footprint overlaps the output area, so a neighbouring path/row
    # that merely falls inside the padding can't hide a real gap.
    cat = _catalog()
    area = _grid_bbox_wgs84(grid)
    l89 = ["landsat-8", "landsat-9"]
    pre_items = _landsat_items(cat, b, pre_start, pre_end, 20, l89)
    post_items = _landsat_items(cat, b, post_start, post_end, 30, l89)
    log.info("Landsat L8/L9 (padded search): pre=%d scenes, post=%d scenes", len(pre_items), len(post_items))
    if not _covering(pre_items, area) or not _covering(post_items, area):
        pre_items = _landsat_items(cat, b, pre_start, pre_end, 20, ["landsat-7"])
        post_items = _landsat_items(cat, b, post_start, post_end, 30, ["landsat-7"])
        log.info("Landsat-7 fallback: pre=%d scenes, post=%d scenes", len(pre_items), len(post_items))
    for label, its, start, end, cloud in (("pre", pre_items, pre_start, pre_end, 20),
                                          ("post", post_items, post_start, post_end, 30)):
        if not _covering(its, area):
            raise RuntimeError(
                f"No Landsat scenes with cloud cover under {cloud}% in the {label}-fire window "
                f"{start} to {end}. Check the fire date ('{fire_date}')."
            )

    dnbr = (_nbr_of_median(pre_items, grid) - _nbr_of_median(post_items, grid)) * 1000
    water = water_mask(grid)
    if not (np.isfinite(dnbr) & ~water).any():
        raise RuntimeError(
            f"dNBR has no valid land pixels for fire date '{fire_date}' "
            f"(pre {pre_start} to {pre_end}, post {post_start} to {post_end})."
        )
    dnbr[np.isnan(dnbr) | water] = WATER_SENTINEL
    _write_grid(dnbr, grid, out_path)


# ── MTBS perimeter ────────────────────────────────────────────────────────────

def fetch_mtbs_perimeter(fire_name: str, fire_date: str) -> gpd.GeoDataFrame | None:
    """
    MTBS burned-area boundary from the USFS EDW ArcGIS REST service — the
    no-auth equivalent of wildcat_fire_prep_service._fetch_mtbs_perimeter().
    Same matching (name contains, upper-case; year from fire_date; largest
    by acres) and the same GEE property names, incl. Ig_Date as epoch-ms.
    """
    from services.wildcat_fire_prep_service import _parse_year

    name_upper = fire_name.upper().strip().replace("'", "''")
    name_where = f"UPPER(fire_name) LIKE '%{name_upper}%'"
    year = _parse_year(fire_date)

    def query(where: str) -> list:
        resp = requests.get(MTBS_QUERY_URL, params={
            "where": where, "outFields": "*", "outSR": 4326, "f": "geojson",
        }, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"MTBS service error: {data['error']}")
        return data.get("features") or []

    features = query(f"{name_where} AND year = {int(year)}") if year else []
    log.info("MTBS query for '%s' (%s): %d features", name_upper, year, len(features))
    if not features:
        # Try without year restriction in case of date uncertainty
        features = query(name_where)
        if not features:
            return None

    gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
    if "acres" not in gdf.columns:
        raise RuntimeError("MTBS service response missing 'acres'; the USFS service schema "
                           f"may have changed ({MTBS_QUERY_URL})")
    row = gdf.sort_values("acres", ascending=False).iloc[[0]].reset_index(drop=True)
    geom = row.geometry.iloc[0]
    if geom is None or geom.is_empty:
        raise RuntimeError("MTBS service response missing 'geometry'; the USFS service schema "
                           f"may have changed ({MTBS_QUERY_URL})")

    def get(col):
        return row[col].iloc[0] if col in row.columns else None

    ig = get("ig_date")
    ig_ms = None
    if ig is not None and not (isinstance(ig, float) and math.isnan(ig)):
        ig_ms = int(pd.Timestamp(str(int(ig))).value // 1_000_000)  # yyyymmdd -> epoch ms
    props = {
        "Event_ID":   get("fire_id"),
        "Incid_Name": get("fire_name"),
        "Incid_Type": get("fire_type"),
        "Ig_Date":    ig_ms,
        "BurnBndAc":  get("acres"),
        "BurnBndLat": get("latitude"),
        "BurnBndLon": get("longitude"),
        "Map_ID":     get("map_id"),
        "Map_Prog":   get("map_prog"),
        "Asmnt_Type": get("asmnt_type"),
        "Pre_ID":     get("pre_id"),
        "Post_ID":    get("post_id"),
        "irwinID":    get("irwinid"),
    }
    return gpd.GeoDataFrame([props], geometry=[geom], crs="EPSG:4326")


def _utm_epsg_from_lon(lon: float) -> int:
    """Return the UTM zone EPSG code (north hemisphere) for a given longitude.
    Same as gee_service._utm_epsg_from_lon."""
    zone = int((lon + 180) / 6) + 1
    return 32600 + zone
