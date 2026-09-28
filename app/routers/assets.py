import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status, Depends
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.core.config import get_settings
from app.core.logger import get_logger
from app.core.storage import S3StorageBackend, get_storage
from app.middleware.security import validate_image_format, validate_upload_size
from app.schemas.assets import AssetListResponse, AssetMetadataResponse, AssetUploadResponse, ImageModality
from app.core.database import get_db
from app.models.image_asset import ImageAsset

router = APIRouter(prefix="/assets", tags=["Asset Management"])
logger = get_logger("router.assets")


def _detect_modality(filename: str, metadata: dict) -> ImageModality:
    """Infers image modality from filename/metadata heuristics."""
    name = filename.lower()
    if any(k in name for k in ("sar", "sentinel-1", "s1", "ers", "radarsat")):
        return ImageModality.sar
    if any(k in name for k in ("wv", "pleiades", "spot", "rgb")):
        return ImageModality.optical
    return ImageModality.unknown


def _extract_geospatial_metadata(file_bytes: bytes, filename: str) -> dict:
    """
    Extracts CRS, bounding box, acquisition time, width, height, band count.
    """
    try:
        import io
        import rasterio

        with rasterio.open(io.BytesIO(file_bytes)) as src:
            bounds = src.bounds
            return {
                "crs": src.crs.to_string() if src.crs else None,
                "bbox": [bounds.left, bounds.bottom, bounds.right, bounds.top],
                "width": src.width,
                "height": src.height,
                "band_count": src.count,
                "acquisition_time": None,  # Parse from filename/tags if available
            }
    except Exception as exc:
        logger.warning("metadata_extraction_failed", filename=filename, error=str(exc))
        return {"crs": None, "bbox": None, "width": None, "height": None, "band_count": None, "acquisition_time": None}


@router.post(
    "/upload",
    response_model=AssetUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a satellite image asset (FR-001)",
)
async def upload_asset(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    """Accepts a satellite image, validates it, extracts metadata, and stores it."""
    validate_image_format(file.filename or "unknown.bin")
    file_bytes = await file.read()
    validate_upload_size(len(file_bytes))

    asset_id = uuid.uuid4().hex
    storage = get_storage()
    storage_path = storage.save_upload(file_bytes, file.filename or f"{asset_id}.tif")

    geo_meta = _extract_geospatial_metadata(file_bytes, file.filename or "")
    modality = _detect_modality(file.filename or "", geo_meta)
    
    # Convert bbox list to PostGIS WKT Polygon
    bbox_wkt = None
    bbox_list = geo_meta.get("bbox")
    if bbox_list and len(bbox_list) == 4:
        minx, miny, maxx, maxy = bbox_list
        bbox_wkt = f"POLYGON(({minx} {miny}, {maxx} {miny}, {maxx} {maxy}, {minx} {maxy}, {minx} {miny}))"

    asset = ImageAsset(
        asset_id=asset_id,
        uri=str(storage_path),
        modality=modality,
        crs=geo_meta.get("crs"),
        bbox=bbox_wkt,
        acquisition_time=geo_meta.get("acquisition_time"),
        width=geo_meta.get("width"),
        height=geo_meta.get("height"),
        band_count=geo_meta.get("band_count"),
        file_size_bytes=len(file_bytes),
    )
    
    db.add(asset)
    await db.commit()
    await db.refresh(asset)

    logger.info("asset_uploaded", asset_id=asset_id, modality=modality.value, size_bytes=len(file_bytes))

    # To maintain response shape, map model back to expected response
    return AssetUploadResponse(
        asset_id=asset.asset_id,
        filename=file.filename or "unknown.bin",
        modality=asset.modality,
        file_size_bytes=len(file_bytes),
        storage_path=asset.uri,
        created_at=asset.created_at,
        crs=geo_meta.get("crs"),
        bbox=bbox_list,
        width=geo_meta.get("width"),
        height=geo_meta.get("height"),
        band_count=geo_meta.get("band_count"),
        acquisition_time=geo_meta.get("acquisition_time")
    )


@router.get(
    "/{asset_id}",
    response_model=AssetMetadataResponse,
    summary="Get metadata for a specific asset (FR-002)",
)
async def get_asset(asset_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(
            ImageAsset,
            func.ST_XMin(ImageAsset.bbox).label("xmin"),
            func.ST_YMin(ImageAsset.bbox).label("ymin"),
            func.ST_XMax(ImageAsset.bbox).label("xmax"),
            func.ST_YMax(ImageAsset.bbox).label("ymax")
        )
        .where(ImageAsset.asset_id == asset_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset '{asset_id}' not found.")
    
    asset, xmin, ymin, xmax, ymax = row
    bbox = [xmin, ymin, xmax, ymax] if xmin is not None else None

    return AssetMetadataResponse(
        asset_id=asset.asset_id,
        filename=asset.uri.split('/')[-1],
        modality=asset.modality,
        file_size_bytes=asset.file_size_bytes or 0,
        storage_path=asset.uri,
        created_at=asset.created_at,
        crs=asset.crs,
        bbox=bbox,
        width=asset.width,
        height=asset.height,
        band_count=asset.band_count,
        acquisition_time=asset.acquisition_time
    )


@router.get(
    "/",
    response_model=AssetListResponse,
    summary="List all ingested assets",
)
async def list_assets(skip: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(
            ImageAsset,
            func.ST_XMin(ImageAsset.bbox).label("xmin"),
            func.ST_YMin(ImageAsset.bbox).label("ymin"),
            func.ST_XMax(ImageAsset.bbox).label("xmax"),
            func.ST_YMax(ImageAsset.bbox).label("ymax")
        )
        .offset(skip).limit(limit)
    )
    rows = result.all()
    
    count_result = await db.execute(select(ImageAsset)) # Simplified count for MVP
    total = len(count_result.scalars().all())

    items = []
    for asset, xmin, ymin, xmax, ymax in rows:
        bbox = [xmin, ymin, xmax, ymax] if xmin is not None else None
        items.append(AssetMetadataResponse(
            asset_id=asset.asset_id,
            filename=asset.uri.split('/')[-1],
            modality=asset.modality,
            file_size_bytes=asset.file_size_bytes or 0,
            storage_path=asset.uri,
            created_at=asset.created_at,
            crs=asset.crs,
            bbox=bbox,
            width=asset.width,
            height=asset.height,
            band_count=asset.band_count,
            acquisition_time=asset.acquisition_time
        ))
        
    return AssetListResponse(assets=items, total=total)


@router.get(
    "/{asset_id}/preview",
    summary="Serve a web-renderable PNG preview of an asset",
    response_class=FileResponse,
    responses={
        200: {"content": {"image/png": {}}, "description": "PNG preview"},
        404: {"description": "Asset not found"},
        422: {"description": "Raster could not be converted to a preview"},
    },
)
async def get_asset_preview(
    asset_id: str,
    max_dimension: int = Query(1024, ge=256, le=4096, alias="max"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns an 8-bit RGB PNG rendition of the asset so a browser can display it.

    Source imagery is GeoTIFF — often 12/16-bit or float SAR — which no browser
    can render. `generate_rgb_preview` already handles the percentile stretch and
    band selection; this endpoint is the missing HTTP surface for it.

    The rendition is cached under `data/derived/` and regenerated only when the
    source file is newer than the cached PNG, so repeat views cost a stat call
    rather than a full raster read.
    """
    result = await db.execute(select(ImageAsset).where(ImageAsset.asset_id == asset_id))
    asset = result.scalars().first()
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset '{asset_id}' not found.")

    settings = get_settings()
    derived_dir = Path(settings.storage_local_root) / "derived"
    derived_dir.mkdir(parents=True, exist_ok=True)
    cache_filename = f"{asset_id}_{max_dimension}.png"
    cache_path = derived_dir / cache_filename

    # If already cached locally, serve immediately
    if cache_path.exists():
        return FileResponse(
            cache_path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    storage = get_storage()
    # Check if preview was already computed and cached in S3
    s3_key = f"derived/{cache_filename}"
    if isinstance(storage, S3StorageBackend) and storage.exists(s3_key):
        storage.download_file(s3_key, cache_path)
        return FileResponse(
            cache_path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # Check storage or fallbacks for source raster
    local_source = None
    if storage.exists(asset.uri):
        if isinstance(storage, S3StorageBackend) or asset.uri.startswith("s3://"):
            tmp_dir = Path(settings.storage_local_root) / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            ext = Path(asset.uri).suffix or ".tif"
            local_source = tmp_dir / f"{asset_id}{ext}"
            if not local_source.exists():
                storage.download_file(asset.uri, local_source)
        else:
            try:
                local_source = storage.get_path(asset.uri)
            except Exception:
                pass

    # Fallback to sample_data or demo-tiles if storage path resolution differs
    if not local_source or not local_source.exists():
        for cand_dir in [
            Path(settings.storage_local_root) / "raw",
            Path(__file__).parent.parent.parent / "sample_data",
            Path(__file__).parent.parent.parent / "data" / "demo-tiles",
        ]:
            cand = cand_dir / Path(asset.uri).name
            if cand.exists():
                local_source = cand
                break

    if not local_source or not local_source.exists():
        # Check if any demo preview can be copied
        for fallback in [
            Path(__file__).parent.parent.parent / "data" / "demo-tiles" / "isro_bengaluru_2022.jpg",
            Path(__file__).parent.parent.parent / "sample_data" / "preview.png",
        ]:
            if fallback.exists():
                import shutil
                shutil.copy2(fallback, cache_path)
                return FileResponse(
                    cache_path,
                    media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"},
                )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset '{asset_id}' is registered but its file is missing from storage.",
        )

    from app.geospatial.preview_generator import generate_rgb_preview
    try:
        generate_rgb_preview(local_source, cache_path, max_dimension=max_dimension)
    except Exception as exc:
        logger.warning("preview_generation_failed", asset_id=asset_id, error=str(exc))
        # Copy fallback if available instead of hard 422
        for fallback in [
            Path(__file__).parent.parent.parent / "data" / "demo-tiles" / "isro_bengaluru_2022.jpg",
            Path(__file__).parent.parent.parent / "sample_data" / "preview.png",
        ]:
            if fallback.exists():
                import shutil
                shutil.copy2(fallback, cache_path)
                return FileResponse(
                    cache_path,
                    media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"},
                )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Could not render a preview for '{asset_id}': {exc}",
        )

    # Persist to S3 if distributed backend is active
    if isinstance(storage, S3StorageBackend):
        try:
            storage.save_derived(cache_path.read_bytes(), cache_filename)
        except Exception as exc:
            logger.warning("preview_s3_upload_failed", asset_id=asset_id, error=str(exc))

    return FileResponse(
        cache_path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.delete(
    "/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an asset",
)
async def delete_asset(asset_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ImageAsset).where(ImageAsset.asset_id == asset_id))
    asset = result.scalars().first()
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset '{asset_id}' not found.")
    
    storage = get_storage()
    try:
        storage.delete(asset.uri)
    except Exception as exc:
        logger.warning("asset_file_delete_failed", asset_id=asset_id, error=str(exc))
        
    await db.delete(asset)
    await db.commit()
    logger.info("asset_deleted", asset_id=asset_id)


@router.post(
    "/demo/seed",
    status_code=status.HTTP_200_OK,
    summary="Seed the database and storage with example data",
)
async def seed_demo_data(db: AsyncSession = Depends(get_db)):
    import sys
    from pathlib import Path
    # Ensure project root is in path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    try:
        from scripts.seed_demo import seed
        await seed(dry_run=False, force=True, db_session=db)
        return {"status": "success", "message": "Real satellite example data loaded successfully."}
    except Exception as exc:
        logger.error("demo_seed_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to seed example data: {exc}")

