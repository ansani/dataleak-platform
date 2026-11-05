#!/usr/bin/env python3
"""
Find all downloaded_files with status REPROCESS, publish them to UNIQUES_QUEUE,
and reset their status to DOWNLOADED so the pipeline re-processes them.

Usage:
    python reprocess_marked.py
    python reprocess_marked.py --dry-run
    python reprocess_marked.py --limit 100
"""

import argparse
import json
import logging

from sqlalchemy import text

import config
from utils import get_db_engine, get_publisher, setup_logging

logger = logging.getLogger(__name__)


def main(dry_run: bool = False, limit: int | None = None):
    setup_logging()

    engine = get_db_engine(config.DB_CONNECTION_STRING)

    with engine.begin() as conn:
        query = text(
            "SELECT id, file_key, saved_file_name FROM downloaded_files "
            "WHERE status = 'REPROCESS' ORDER BY id"
        )
        rows = conn.execute(query).fetchall()

    if not rows:
        logger.info("No records with status REPROCESS found.")
        return

    if limit:
        rows = rows[:limit]

    logger.info("Found %d records with status REPROCESS.", len(rows))

    if dry_run:
        for row in rows:
            logger.info(
                "[DRY-RUN] Would publish id=%s file_key=%s", row.id, row.file_key
            )
        return

    publisher = get_publisher(config.RABBIT_CONNECTION_STRING, config.UNIQUES_QUEUE)

    ok = 0
    fail = 0

    for row in rows:
        message = {
            "id": row.id,
            "file_key": row.file_key,
            "saved_file_name": row.saved_file_name,
        }
        if publisher.publish(message):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE downloaded_files SET status = 'DOWNLOADED' WHERE id = :id"
                    ),
                    {"id": row.id},
                )
            logger.info("Published + reset id=%s", row.id)
            ok += 1
        else:
            logger.error("Failed to publish id=%s", row.id)
            with open("failed_publish.json", "a") as f:
                json.dump(message, f)
                f.write("\n")
            fail += 1

    logger.info("Done: %d published, %d failed.", ok, fail)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Re-publish REPROCESS records to the pipeline"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="List records without publishing"
    )
    parser.add_argument("--limit", type=int, help="Max number of records to process")
    args = parser.parse_args()
    main(dry_run=args.dry_run, limit=args.limit)
