#!/usr/bin/env python3
"""
SatQuery AI — Example Data Seeder
=================================
Populates the database and storage with realistic bi-temporal satellite imagery
(ISRO 2022 optical baseline vs. 2026 optical change + 2026 SAR radar observation)
along with pre-computed change detection findings, WGS84 geometries, and reports.

Usage:
    python3 scripts/seed_demo.py                # seed with sample_data/
    python3 scripts/seed_demo.py --reset        # wipe existing seed data first
    python3 scripts/seed_demo.py --dry-run      # print summary without writing
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import sys
import uuid
from datetime import datetime
from pathlib import Path

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.logger import get_logger, setup_logging
from app.core.storage import get_storage
from app.geospatial.preview_generator import generate_rgb_preview
from app.models.analysis_run import AnalysisRun
from app.models.finding import Finding
from app.models.image_asset import ImageAsset
from app.models.query import Query
from app.models.report import Report
from app.models.session import Session
from app.schemas.assets import ImageModality
from app.schemas.sessions import SessionState
from app.schemas.workflows import WorkflowType

setup_logging()
logger = get_logger("seed_demo")

SAMPLE_DATA = Path(__file__).parent.parent / "sample_data"

# ── Deterministic IDs so re-running or referencing is predictable ─────────────
DEMO_SESSION_ID       = "demo0000000000000000000000000001"
DEMO_ASSET_OPT_22_ID  = "demo0000000000000000000000000002"
DEMO_ASSET_OPT_26_ID  = "demo0000000000000000000000000003"
DEMO_ASSET_SAR_26_ID  = "demo0000000000000000000000000004"

DEMO_QUERY_CD_ID      = "demo0000000000000000000000000005"
DEMO_QUERY_SAR_ID     = "demo0000000000000000000000000006"
DEMO_RUN_CD_ID        = "demo0000000000000000000000000007"
DEMO_RUN_SAR_ID       = "demo0000000000000000000000000008"
DEMO_REPORT_CD_ID     = "demo0000000000000000000000000009"
DEMO_REPORT_SAR_ID    = "demo0000000000000000000000000010"

# Real WGS84 footprint: Bengaluru East (Whitefield/KR Puram/Sarjapur corridor)
# MODIS Terra 250m tiles — zoom 8, tile (183, 109) @ EPSG:4326
# Area: 12.90°N-13.10°N, 77.55°E-77.75°E
BBOX_WKT = "POLYGON((77.55 12.90, 77.75 12.90, 77.75 13.10, 77.55 13.10, 77.55 12.90))"

# Finding 1: New IT/Tech Corridor Urban Expansion (Whitefield area NE sector)
# Pixel box: [512, 256, 900, 700]
FINDING_1_WKT = "POLYGON((77.65 12.95, 77.72 12.95, 77.72 13.02, 77.65 13.02, 77.65 12.95))"

# Finding 2: Road Infrastructure and Residential Expansion (Sarjapur area)
# Pixel box: [100, 500, 450, 850]
FINDING_2_WKT = "POLYGON((77.57 12.90, 77.64 12.90, 77.64 12.97, 77.57 12.97, 77.57 12.90))"

# Finding 3: High-Density Built Structure Cluster — SAR Fusion (KR Puram zone)
FINDING_3_WKT = "POLYGON((77.65 12.95, 77.72 12.95, 77.72 13.02, 77.65 13.02, 77.65 12.95))"

# Finding 4: Bellandur Lake (SAR specular reflection from water body)
FINDING_4_WKT = "POLYGON((77.65 12.90, 77.75 12.90, 77.75 12.97, 77.65 12.97, 77.65 12.90))"


async def wipe_seed_data(db: AsyncSession) -> None:
    """Deletes all records with demo IDs."""
    logger.info("wiping_existing_seed_data")

    # Delete in reverse FK dependency order
    await db.execute(text("DELETE FROM reports WHERE report_id LIKE 'demo%'"))
    await db.execute(text("DELETE FROM findings WHERE run_id LIKE 'demo%'"))
    await db.execute(text("DELETE FROM analysis_runs WHERE run_id LIKE 'demo%'"))
    await db.execute(text("DELETE FROM queries WHERE query_id LIKE 'demo%'"))
    await db.execute(text("DELETE FROM image_assets WHERE asset_id LIKE 'demo%'"))
    await db.execute(text("DELETE FROM sessions WHERE session_id LIKE 'demo%'"))
    await db.commit()
    logger.info("seed_data_wiped")


def _find_sample_file(filename: str) -> Path:
    candidates = [
        SAMPLE_DATA / filename,
        Path("/app/sample_data") / filename,
        Path(__file__).parent.parent / "sample_data" / filename,
        Path(__file__).parent.parent / "data" / "raw" / filename,
        Path("/app/data/raw") / filename,
        Path(__file__).parent.parent / "data" / "demo-tiles" / filename,
        Path("/app/data/demo-tiles") / filename,
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return c
    return candidates[0]


async def seed(dry_run: bool = False, force: bool = False, db_session: AsyncSession | None = None) -> None:
    settings = get_settings()
    engine = None
    if db_session is None:
        engine = create_async_engine(settings.database_url, echo=False)
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        db_cm = session_factory()
    else:
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def _use_existing(s):
            yield s
        db_cm = _use_existing(db_session)

    storage = get_storage()

    async with db_cm as db:
        existing = await db.execute(select(Session).where(Session.session_id == DEMO_SESSION_ID))
        if existing.scalars().first():
            if not force:
                logger.info("seed_already_present", msg="Demo data already exists. Wiping to ensure real imagery...")
            await wipe_seed_data(db)

        logger.info("seeding_example_data", dry_run=dry_run)

        # ── 1. Upload satellite GeoTIFF files to storage ────────────────────────
        opt_22_file = _find_sample_file("isro_optical_2022.tif")
        opt_26_file = _find_sample_file("isro_optical_2026.tif")
        sar_26_file = _find_sample_file("isro_sar_2026.tif")

        def _upload(path: Path, subdir: str = "raw") -> str:
            if not path.exists():
                logger.warning("sample_file_missing", path=str(path))
                return f"local://{path}"
            data = path.read_bytes()
            if dry_run:
                return f"local://{path}"
            result = storage.save_upload(data, path.name, subdir=subdir, preserve_name=True)
            return str(result)

        opt_22_uri = _upload(opt_22_file)
        opt_26_uri = _upload(opt_26_file)
        sar_26_uri = _upload(sar_26_file)
        logger.info("assets_uploaded", opt_2022=opt_22_uri, opt_2026=opt_26_uri, sar_2026=sar_26_uri)

        if dry_run:
            _print_dry_run_summary(opt_22_uri, opt_26_uri, sar_26_uri)
            return

        # ── 2. Pre-generate web-renderable PNG previews ───────────────────────
        import shutil
        derived_dir = Path(settings.storage_local_root) / "derived"
        derived_dir.mkdir(parents=True, exist_ok=True)
        preview_sources = [
            (DEMO_ASSET_OPT_22_ID, "isro_bengaluru_2022.jpg", opt_22_file),
            (DEMO_ASSET_OPT_26_ID, "isro_bengaluru_2026.jpg", opt_26_file),
            (DEMO_ASSET_SAR_26_ID, "isro_sar.png", sar_26_file),
        ]
        for asset_id, demo_tile_name, src_file in preview_sources:
            target_preview = derived_dir / f"{asset_id}_1024.png"
            if not target_preview.exists():
                try:
                    copied = False
                    for search_dir in [
                        Path(__file__).parent.parent / "data" / "demo-tiles",
                        Path("/app/data/demo-tiles"),
                        Path(__file__).parent.parent / "sample_data",
                        Path("/app/sample_data"),
                    ]:
                        cand = search_dir / demo_tile_name
                        if cand.exists():
                            shutil.copy2(cand, target_preview)
                            copied = True
                            break
                    if not copied:
                        for search_dir in [
                            Path(__file__).parent.parent / "sample_data",
                            Path("/app/sample_data"),
                        ]:
                            cand = search_dir / "preview.png"
                            if cand.exists():
                                shutil.copy2(cand, target_preview)
                                copied = True
                                break
                    if not copied and src_file.exists() and src_file.suffix.lower() == ".tif":
                        generate_rgb_preview(src_file, target_preview, max_dimension=1024)
                except Exception as exc:
                    logger.warning("pregenerate_previews_error", asset_id=asset_id, error=str(exc))

        # ── 3. Session ────────────────────────────────────────────────────────
        demo_session = Session(
            session_id=DEMO_SESSION_ID,
            state=SessionState.active,
            conversation_history=[
                {
                    "role": "user",
                    "content": "Compare the 2019 and 2024 satellite observations to detect land cover changes and urban development in Bengaluru.",
                },
                {
                    "role": "assistant",
                    "content": (
                        "Bi-temporal change detection complete. MODIS Terra 250m imagery comparison for Bengaluru East "
                        "(Whitefield/Sarjapur corridor) reveals significant urban expansion between February 2019 and February 2024. "
                        "The Whitefield IT corridor and Sarjapur Road zones show a mean spectral change index of 89.5, "
                        "indicating major impervious surface growth. Two primary change regions identified: "
                        "the Whitefield EPIP Zone tech-park expansion (est. 68 ha) and the Sarjapur Road residential densification (est. 52 ha)."
                    ),
                },
            ],
        )
        db.add(demo_session)
        await db.flush()

        # ── 4. Image Assets ───────────────────────────────────────────────────
        # Asset 1: 2019 Optical baseline observation (MODIS Terra, Bengaluru)
        db.add(ImageAsset(
            asset_id=DEMO_ASSET_OPT_22_ID,
            uri=opt_22_uri,
            modality=ImageModality.optical,
            crs="EPSG:4326",
            bbox=BBOX_WKT,
            acquisition_time=datetime(2019, 2, 10, 5, 30, 0),
            width=1024,
            height=1024,
            band_count=3,
            file_size_bytes=opt_22_file.stat().st_size if opt_22_file.exists() else 0,
        ))

        # Asset 2: 2024 Optical follow-up observation (urban expansion clearly visible)
        db.add(ImageAsset(
            asset_id=DEMO_ASSET_OPT_26_ID,
            uri=opt_26_uri,
            modality=ImageModality.optical,
            crs="EPSG:4326",
            bbox=BBOX_WKT,
            acquisition_time=datetime(2024, 2, 10, 5, 30, 0),
            width=1024,
            height=1024,
            band_count=3,
            file_size_bytes=opt_26_file.stat().st_size if opt_26_file.exists() else 0,
        ))

        # Asset 3: 2024 Dual-polarization SAR radar observation (derived from MODIS)
        db.add(ImageAsset(
            asset_id=DEMO_ASSET_SAR_26_ID,
            uri=sar_26_uri,
            modality=ImageModality.sar,
            crs="EPSG:4326",
            bbox=BBOX_WKT,
            acquisition_time=datetime(2024, 2, 11, 6, 15, 0),
            width=1024,
            height=1024,
            band_count=2,
            file_size_bytes=sar_26_file.stat().st_size if sar_26_file.exists() else 0,
        ))
        await db.flush()
        logger.info("seeded_image_assets")

        # ── 5. Query 1: Bi-temporal Change Detection (2019 vs 2024) ───────────
        db.add(Query(
            query_id=DEMO_QUERY_CD_ID,
            session_id=DEMO_SESSION_ID,
            text="Compare the 2019 and 2024 satellite observations to detect land cover changes and urban development in Bengaluru.",
            referenced_assets=[DEMO_ASSET_OPT_22_ID, DEMO_ASSET_OPT_26_ID],
            status="completed",
        ))
        await db.flush()

        db.add(AnalysisRun(
            run_id=DEMO_RUN_CD_ID,
            query_id=DEMO_QUERY_CD_ID,
            workflow=WorkflowType.change_detection,
            tools_used=[],
            status="completed",
            duration_ms=3140.0,
            trace=[
                {"step": "router", "decision": "bi_temporal_change_detection"},
                {"step": "co_registration", "status": "aligned_epsg_4326"},
                {"step": "gemini_vision_vlm", "model": "gemini-2.5-flash", "status": "detections_extracted"},
                {"step": "coordinate_transform", "status": "verified_wgs84"},
            ],
        ))
        await db.flush()

        # Finding 1: IT Corridor Urban Expansion — Whitefield/EPIP Zone
        db.add(Finding(
            finding_id=uuid.uuid4().hex,
            run_id=DEMO_RUN_CD_ID,
            geometry=FINDING_1_WKT,
            label="IT Corridor Urban Expansion (Whitefield)",
            answer="Major tech-park and residential complex built between 2019 and 2024 in the Whitefield/EPIP Zone — one of Bengaluru's fastest-growing corridors. MODIS 250m imagery confirms significant increase in high-reflectance impervious surfaces.",
            confidence=0.96,
            evidence_refs=[],
            properties={
                "workflow": "change_detection",
                "change_type": "Urban Expansion / Impervious Surface Growth",
                "before_state": "Mixed vegetation and sparse settlements (2019)",
                "after_state": "Dense IT park campus and multi-story residential towers (2024)",
                "estimated_area_m2": 680000,
                "bounding_boxes": [
                    {"x_min": 512, "y_min": 256, "x_max": 900, "y_max": 700}
                ],
            },
        ))

        # Finding 2: Sarjapur Road Corridor Densification
        db.add(Finding(
            finding_id=uuid.uuid4().hex,
            run_id=DEMO_RUN_CD_ID,
            geometry=FINDING_2_WKT,
            label="Sarjapur Road Corridor Densification",
            answer="The Sarjapur Road corridor shows clear densification of built-up area between 2019 and 2024, with new apartment complexes and commercial strips replacing agricultural and vacant land.",
            confidence=0.93,
            evidence_refs=[],
            properties={
                "workflow": "change_detection",
                "change_type": "Residential & Commercial Densification",
                "before_state": "Agricultural plots and sparse low-rise structures (2019)",
                "after_state": "High-density residential apartments and commercial zones (2024)",
                "estimated_area_m2": 520000,
                "bounding_boxes": [
                    {"x_min": 100, "y_min": 500, "x_max": 450, "y_max": 850}
                ],
            },
        ))
        await db.flush()

        db.add(Report(
            report_id=DEMO_REPORT_CD_ID,
            run_id=DEMO_RUN_CD_ID,
            session_id=DEMO_SESSION_ID,
            summary=(
                "Bi-temporal MODIS Terra satellite analysis (250m resolution) between February 2019 and February 2024 "
                "for the Bengaluru East urban corridor reveals substantial anthropogenic transformation. "
                "The Whitefield IT corridor and Sarjapur Road zone have experienced rapid impervious surface growth: "
                "tech parks, apartment towers, and commercial strips now occupy areas that were vegetation and farmland in 2019. "
                "Mean spectral change index: 89.5 (MODIS band composite). Bellandur Lake boundaries remain identifiable."
            ),
            evidence=[
                {"type": "image", "asset_id": DEMO_ASSET_OPT_22_ID, "label": "MODIS Terra Baseline (Feb 2019)"},
                {"type": "image", "asset_id": DEMO_ASSET_OPT_26_ID, "label": "MODIS Terra Observation (Feb 2024)"},
            ],
        ))
        await db.flush()
        logger.info("seeded_change_detection_run")

        # ── 6. Query 2: SAR-Optical Multi-Modal Fusion ────────────────────────
        db.add(Query(
            query_id=DEMO_QUERY_SAR_ID,
            session_id=DEMO_SESSION_ID,
            text="Analyze radar backscatter and optical characteristics using SAR and MODIS optical fusion for the Bengaluru urban area.",
            referenced_assets=[DEMO_ASSET_OPT_26_ID, DEMO_ASSET_SAR_26_ID],
            status="completed",
        ))
        await db.flush()

        db.add(AnalysisRun(
            run_id=DEMO_RUN_SAR_ID,
            query_id=DEMO_QUERY_SAR_ID,
            workflow=WorkflowType.sar_fusion,
            tools_used=[],
            status="completed",
            duration_ms=4820.0,
            trace=[
                {"step": "router", "decision": "sar_optical_fusion"},
                {"step": "sar_backscatter_calibration", "polarizations": ["VV", "VH"]},
                {"step": "gemini_multimodal_synthesis", "model": "gemini-2.5-pro", "status": "fused_signatures_analyzed"},
            ],
        ))
        await db.flush()

        db.add(Finding(
            finding_id=uuid.uuid4().hex,
            run_id=DEMO_RUN_SAR_ID,
            geometry=FINDING_3_WKT,
            label="High-Density Built Structure Cluster",
            answer="Strong double-bounce microwave backscatter co-located with high-reflectance optical rooftops confirms newly erected multi-story buildings.",
            confidence=0.94,
            evidence_refs=[],
            properties={
                "workflow": "sar_fusion",
                "sar_evidence": "Intense double-bounce return (>0.82 normalized intensity)",
                "optical_evidence": "Rectilinear geometric rooftop signatures",
                "bounding_boxes": [
                    {"x_min": 80, "y_min": 180, "x_max": 650, "y_max": 480}
                ],
            },
        ))

        db.add(Finding(
            finding_id=uuid.uuid4().hex,
            run_id=DEMO_RUN_SAR_ID,
            geometry=FINDING_4_WKT,
            label="Meandering River Channel",
            answer="Very low radar backscatter due to specular reflection off smooth water surface confirms open water river channel.",
            confidence=0.95,
            evidence_refs=[],
            properties={
                "workflow": "sar_fusion",
                "sar_evidence": "Near-zero microwave return (<0.08 normalized intensity)",
                "optical_evidence": "Dark winding natural watercourse with riparian tree margins",
                "bounding_boxes": [
                    {"x_min": 20, "y_min": 10, "x_max": 980, "y_max": 280}
                ],
            },
        ))
        await db.flush()

        db.add(Report(
            report_id=DEMO_REPORT_SAR_ID,
            run_id=DEMO_RUN_SAR_ID,
            session_id=DEMO_SESSION_ID,
            summary=(
                "SAR-Optical multimodal fusion analysis for Bengaluru East (2024) identifies dense urban build-up "
                "in the Whitefield and KR Puram zones via high backscatter double-bounce signatures. "
                "The Bellandur Lake water body is confirmed via near-zero SAR specular reflection co-located "
                "with dark optical spectral tone. Fusion confidence: 0.91."
            ),
            evidence=[
                {"type": "image", "asset_id": DEMO_ASSET_OPT_26_ID, "label": "MODIS Optical (Feb 2024)"},
                {"type": "image", "asset_id": DEMO_ASSET_SAR_26_ID, "label": "SAR Radar (Feb 2024)"},
            ],
        ))
        await db.commit()
        logger.info("seeded_sar_fusion_run")

    if engine is not None:
        await engine.dispose()
    print("\n✅  Real-world satellite example data seeded successfully!")
    print(f"   Session ID:         {DEMO_SESSION_ID}")
    print(f"   MODIS 2019 optical: {DEMO_ASSET_OPT_22_ID} (Bengaluru East baseline)")
    print(f"   MODIS 2024 optical: {DEMO_ASSET_OPT_26_ID} (Bengaluru East observation)")
    print(f"   SAR Radar 2024:     {DEMO_ASSET_SAR_26_ID} (Multi-modal)")
    print(f"   Reports:            {DEMO_REPORT_CD_ID} / {DEMO_REPORT_SAR_ID}")
    print("\n   Area: Bengaluru East (Whitefield/Sarjapur/KR Puram), 12.90-13.10°N, 77.55-77.75°E")
    print("   Open http://localhost:3000/compare to view the real-world satellite comparison.")


def _print_dry_run_summary(opt_22: str, opt_26: str, sar_26: str) -> None:
    print("\n🔎  DRY RUN — no data was written.\n")
    print("   Would create:")
    print(f"     Session        {DEMO_SESSION_ID}")
    print(f"     ImageAsset     {DEMO_ASSET_OPT_22_ID} (Optical 2022) -> {opt_22}")
    print(f"     ImageAsset     {DEMO_ASSET_OPT_26_ID} (Optical 2026) -> {opt_26}")
    print(f"     ImageAsset     {DEMO_ASSET_SAR_26_ID} (SAR Radar 2026) -> {sar_26}")
    print(f"     Query          {DEMO_QUERY_CD_ID}  (change_detection)")
    print(f"     Query          {DEMO_QUERY_SAR_ID}  (sar_fusion)")
    print(f"     AnalysisRun    {DEMO_RUN_CD_ID}")
    print(f"     AnalysisRun    {DEMO_RUN_SAR_ID}")
    print(f"     4 Findings     (2 per run, with WGS84 and pixel bounding boxes)")
    print(f"     Report         {DEMO_REPORT_CD_ID}")
    print(f"     Report         {DEMO_REPORT_SAR_ID}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed SatQuery AI with realistic satellite example data.")
    parser.add_argument("--reset", action="store_true", help="Wipe existing seed data first")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be inserted without writing")
    args = parser.parse_args()

    async def _main():
        if args.reset and not args.dry_run:
            settings = get_settings()
            engine = create_async_engine(settings.database_url, echo=False)
            session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with session_factory() as db:
                await wipe_seed_data(db)
            await engine.dispose()
        await seed(dry_run=args.dry_run, force=args.reset)

    asyncio.run(_main())
