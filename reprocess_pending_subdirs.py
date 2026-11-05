#!/usr/bin/env python3
"""
Re-publish all subdirectories with status PENDING to the SUBDIR_QUEUE
so that analyze.py workers can pick them up.

Usage:
    python reprocess_pending_subdirs.py
    python reprocess_pending_subdirs.py --dry-run      # just list without publishing
"""

import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy import text

import config
from utils import get_db_engine, get_publisher, get_real_extension, setup_logging

logger = logging.getLogger(__name__)


def _archive_stem(filename: str) -> str:
    ext = get_real_extension(filename)
    suffix = ext.lower()
    base = filename[: -len(ext)] if ext else filename
    name = Path(base).stem
    if suffix in (".zip", ".rar", ".7z") and name.lower().endswith(suffix):
        name = name[: -len(suffix)]
    return name


def _resolve_subdir_path(archive_stem: str, subdir_name: str) -> str | None:
    """Search for subdir_name under UNCOMPRESS_DIR/<archive_stem>/.

    Handles both flat extraction and single-subdir unwrapping.
    Returns the full filesystem path or None.
    """
    base = Path(config.UNCOMPRESS_DIR) / archive_stem
    if not base.is_dir():
        return None

    direct = base / subdir_name
    if direct.is_dir():
        return str(direct.resolve())

    for child in base.iterdir():
        if child.is_dir():
            candidate = child / subdir_name
            if candidate.is_dir():
                return str(candidate.resolve())

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Re-publish PENDING subdirectories to the analysis queue"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without publishing",
    )
    args = parser.parse_args()

    setup_logging()
    engine = get_db_engine()

    logger.info("Fetching PENDING subdirectories...")

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT s.id, s.downloaded_file_id, s.subdirectory, df.saved_file_name
                FROM subdirectories s
                JOIN downloaded_files df ON df.id = s.downloaded_file_id
                WHERE s.status = 'PENDING'
                ORDER BY s.id
            """)
        ).fetchall()

    if not rows:
        logger.info("No PENDING subdirectories found.")
        return 0

    logger.info("Found %d PENDING subdirectories.", len(rows))

    if args.dry_run:
        dry_count = 0
        for sid, fid, subdir_name, saved_name in rows:
            archive_stem = _archive_stem(saved_name)
            resolved = _resolve_subdir_path(archive_stem, subdir_name)
            status = "OK" if resolved else "DIR NOT FOUND"
            print(
                f"  subdir_id={sid}  file_id={fid}  "
                f"subdir={subdir_name}  archive={saved_name}  stem={archive_stem}  [{status}]"
            )
            dry_count += 1
        print(f"\nDry-run complete. {dry_count} subdirectories would be published.")
        return 0

    publisher = get_publisher(config.RABBIT_CONNECTION_STRING, config.SUBDIR_QUEUE)
    published = 0
    not_found = 0
    errors = 0

    for sid, fid, subdir_name, saved_name in rows:
        archive_stem = _archive_stem(saved_name)
        resolved = _resolve_subdir_path(archive_stem, subdir_name)

        if not resolved:
            logger.warning(
                "Subdirectory not found on disk: subdir_id=%s subdir=%s archive=%s",
                sid,
                subdir_name,
                saved_name,
            )
            not_found += 1
            continue

        msg = {
            "subdir_id": sid,
            "file_id": fid,
            "complete_subdir_path": resolved,
        }

        try:
            if publisher.publish(msg):
                logger.info(
                    "Published subdir_id=%s file_id=%s path=%s",
                    sid,
                    fid,
                    resolved,
                )
                published += 1
            else:
                logger.error("Failed to publish subdir_id=%s", sid)
                errors += 1
        except Exception as e:
            logger.error("Error publishing subdir_id=%s: %s", sid, e)
            errors += 1

    logger.info(
        "Done. Published=%d  Not_found=%d  Errors=%d",
        published,
        not_found,
        errors,
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
