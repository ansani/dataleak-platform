#!/usr/bin/env python3
"""
Find all downloaded_files with status TODUMP_PASSWORD and enqueue them
into COMBO_QUEUE so the ULP import worker can process them.

If a file is still in DOWNLOAD_DIR rather than PASSWORD_DUMP_DIR, move it first.

Usage:
    python enqueue_todump_password.py
"""

import logging
import os
import shutil
import sys

from sqlalchemy import text

import config
from utils import (
    get_db_engine,
    get_publisher,
    handle_failed_publish,
    local_download_path,
    password_dump_path,
    setup_logging,
)

logger = logging.getLogger(__name__)


def fetch_todump_records(engine):
    """Return all rows from downloaded_files with status TODUMP_PASSWORD."""
    query = text("""
        SELECT id, saved_file_name, file_key
        FROM downloaded_files
        WHERE status = 'TODUMP_PASSWORD'
        ORDER BY id
    """)
    with engine.begin() as conn:
        return conn.execute(query).fetchall()


def ensure_file_in_dump_dir(file_id, saved_name):
    """Return the path in PASSWORD_DUMP_DIR, moving the file there if needed."""
    dst = password_dump_path(saved_name)

    if os.path.isfile(dst):
        return dst

    src = local_download_path(saved_name)
    if not os.path.isfile(src):
        logger.warning(
            "[id=%d] File not found in downloads or password dumps: %s",
            file_id,
            saved_name,
        )
        return None

    os.makedirs(config.PASSWORD_DUMP_DIR, exist_ok=True)
    shutil.move(src, dst)
    logger.info("[id=%d] Moved %s -> %s", file_id, src, dst)
    return dst


def main():
    setup_logging()
    engine = get_db_engine()
    publisher = get_publisher(config.RABBIT_CONNECTION_STRING, config.COMBO_QUEUE)

    records = fetch_todump_records(engine)
    logger.info("Found %d records with status TODUMP_PASSWORD", len(records))

    if not records:
        print("No records with status TODUMP_PASSWORD found.")
        return 0

    enqueued = 0
    skipped = 0
    errors = 0

    for row in records:
        file_id, saved_name, file_key = row

        dst_path = ensure_file_in_dump_dir(file_id, saved_name)
        if dst_path is None:
            skipped += 1
            continue

        message = {"id": file_id, "file_path": dst_path}
        if publisher.publish(message):
            enqueued += 1
            logger.info(
                "[id=%d] Enqueued to %s: %s", file_id, config.COMBO_QUEUE, saved_name
            )
        else:
            errors += 1
            handle_failed_publish(
                message, os.path.join(config.DOWNLOAD_DIR, "failed_publish.json")
            )
            logger.error("[id=%d] Failed to enqueue to %s", file_id, config.COMBO_QUEUE)

    print(
        f"\nDone: {enqueued} enqueued, {skipped} skipped (file missing), {errors} publish errors"
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
