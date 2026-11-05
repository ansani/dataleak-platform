#!/usr/bin/env python3
"""
Deep scan subdirectories with NOMAIL status, scanning ALL text files (including
ok/ko/suspect/leaks/nomail subdirs) for email addresses previously missed.

Usage: python deep_nomail_scan.py

Processes all entries in batch without user confirmation. Emails found are
shown on screen. If all emails were already sent, the entry status is set
directly to PROCESSED_EMAILS_ALREADY_SENT; otherwise it is re-queued to
SUBDIR_QUEUE for re-analysis.
"""

import json
import logging
import re
import sys
import time
from pathlib import Path

from sqlalchemy import text

import config
from utils import (
    AnalysisManager,
    RabbitMQPublisher,
    clean_string,
    get_db_engine,
    normalize_and_filter_emails,
    path_contains_ignored_part,
    setup_logging,
)

logger = logging.getLogger(__name__)
engine = get_db_engine()

BINARY_EXTENSIONS = frozenset({
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".svg",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst",
    ".pyc", ".pyo", ".pyd",
    ".sqlite", ".sqlite-shm", ".sqlite-wal",
    ".db", ".db-shm", ".db-wal",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv",
    ".ttf", ".otf", ".woff", ".woff2",
})


def deep_scan_for_emails(directory: str) -> dict[str, list[str]]:
    """Walk ALL files and return {email: [file_path, ...]} mapping."""
    email_files: dict[str, list[str]] = {}
    root = Path(directory)

    if not root.exists():
        logger.error("Directory not found: %s", directory)
        return email_files

    logger.info("Deep scanning directory: %s", directory)

    files = [f for f in root.rglob("*") if f.is_file()]
    if not files:
        logger.warning("No files found in %s", directory)
        return email_files

    logger.info("Found %d files to scan", len(files))
    scanned = 0

    for file_path in files:
        if file_path.suffix.lower() in BINARY_EXTENSIONS:
            continue

        if path_contains_ignored_part(file_path):
            logger.debug("Skipping cookie-related path: %s", file_path)
            continue

        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = clean_string(f.read())

            if not content:
                continue

            email_matches = re.findall(
                r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", content
            )
            for email in email_matches:
                cleaned = clean_string(email).lower()
                if cleaned not in email_files:
                    email_files[cleaned] = []
                str_path = str(file_path)
                if str_path not in email_files[cleaned]:
                    email_files[cleaned].append(str_path)

            scanned += 1
        except Exception:
            continue

    logger.info(
        "Scanned %d/%d files, found %d raw emails in %s",
        scanned, len(files), len(email_files), directory,
    )
    return email_files


def main():
    setup_logging()
    logger.info("Starting deep NOMAIL scan")

    query = text("""
        SELECT al.id, al.analyzed_dir, al.parent_path, al.country,
               al.subdirectory_id, s.downloaded_file_id
        FROM analyzed_leaks al
        JOIN subdirectories s ON s.id = al.subdirectory_id
        WHERE al.status IN ('NOMAIL', 'PROCESSED_NOMAIL')
        ORDER BY al.id
    """)

    try:
        with engine.begin() as conn:
            rows = conn.execute(query).fetchall()

        if not rows:
            print("\nNo NOMAIL entries found in analyzed_leaks table.")
            return

        total = len(rows)
        deleted_ids = []
        requeued_ids = []
        print(f"\nFound {total} NOMAIL entr{'y' if total == 1 else 'ies'} to scan.\n")

        publisher = RabbitMQPublisher(config.RABBIT_CONNECTION_STRING, config.SUBDIR_QUEUE)

        for idx, row in enumerate(rows, 1):
            record_id, analyzed_dir, parent_path, country, subdir_id, file_id = row

            print(f"\n{'=' * 80}")
            print(f"Entry {idx}/{total}")
            print(f"  analyzed_leaks.id  : {record_id}")
            print(f"  subdirectory_id    : {subdir_id}")
            print(f"  downloaded_file_id : {file_id}")
            print(f"  analyzed_dir       : {analyzed_dir}")
            print(f"  parent_path        : {parent_path}")
            print(f"  country            : {country}")
            print(f"{'=' * 80}")

            if not Path(analyzed_dir).exists():
                deleted_ids.append(record_id)
                print(f"\n  \u274c Directory does not exist on disk. Will be deleted.\n")
                continue

            t0 = time.time()
            email_files = deep_scan_for_emails(analyzed_dir)
            elapsed = time.time() - t0

            if email_files:
                valid = normalize_and_filter_emails(email_files.keys())
                print(f"\n  \u2705 Found {len(email_files)} raw / {len(valid)} valid email(s) (in {elapsed:.1f}s):\n")
                for email in valid:
                    print(f"    \u2022 {email}")
                    if len(email_files[email]) == 1:
                        print(f"        \u2192 {email_files[email][0]}")
                    else:
                        print(f"        \u2192 ({len(email_files[email])} files):")
                        for fpath in email_files[email]:
                            print(f"          - {fpath}")

                am = AnalysisManager(engine)
                already_sent = sum(1 for e in valid if am.has_been_sent(e))

                print(f"\n    Sent emails: {already_sent}/{len(valid)}")

                if already_sent == len(valid):
                    am.update_leak_status(analyzed_dir, "PROCESSED_EMAILS_ALREADY_SENT")
                    print(f"  \u2705 Status set to PROCESSED_EMAILS_ALREADY_SENT (all emails already sent).")
                else:
                    msg = {
                        "file_id": file_id,
                        "subdir_id": subdir_id,
                        "complete_subdir_path": analyzed_dir,
                    }
                    ok = publisher.publish(msg)
                    if ok:
                        requeued_ids.append(record_id)
                        print(f"\n  \U0001f504 Re-queued to {config.SUBDIR_QUEUE} for re-analysis.")
                    else:
                        print(f"\n  \u274c Failed to re-queue to {config.SUBDIR_QUEUE}.")
            else:
                print(f"\n  \u274c No emails found (in {elapsed:.1f}s).\n")

        publisher.close()

        if deleted_ids:
            with engine.begin() as conn:
                for rid in deleted_ids:
                    conn.execute(
                        text("DELETE FROM analyzed_leaks WHERE id = :id"),
                        {"id": rid},
                    )
            print(f"\n\u274c Deleted {len(deleted_ids)} stale record{'s' if len(deleted_ids) != 1 else ''} from analyzed_leaks.")

        if requeued_ids:
            print(f"\n\U0001f504 Re-queued {len(requeued_ids)} entr{'y' if len(requeued_ids) == 1 else 'ies'} to {config.SUBDIR_QUEUE} for re-analysis.")

        print(f"\n{'=' * 80}")
        print(f"Summary: {total - len(deleted_ids)} scanned, {len(requeued_ids)} re-queued, {len(deleted_ids)} deleted.")

    except Exception as e:
        logger.error("Error: %s", e)
        print(f"\n\u274c Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
