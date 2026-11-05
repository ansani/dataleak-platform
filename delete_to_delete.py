#!/usr/bin/env python3
"""
Script to delete all files marked with status TO_DELETE from the filesystem
and, upon successful deletion, mark their DB records as DELETED (keeping
the record to prevent re-download if the same file is re-discovered).

Usage: python delete_to_delete.py [-y]
"""

import argparse
import logging
import os
import sys

from sqlalchemy import text

from config import DOWNLOAD_DIR, PASSWORD_DUMP_DIR
from utils import get_db_engine, send_simple_email, setup_logging

logger = logging.getLogger(__name__)


def fetch_to_delete_files(engine):
    """Fetch all records from downloaded_files with status TO_DELETE."""
    query = text("""
        SELECT id, file_key, saved_file_name, channel_id, downloaded_at
        FROM downloaded_files
        WHERE status = 'TO_DELETE'
        ORDER BY id
    """)

    with engine.begin() as conn:
        results = conn.execute(query).fetchall()

    return results


def _safe_path(saved_name, base_dir=None):
    """
    Resolve the absolute path for saved_name inside base_dir (or DOWNLOAD_DIR)
    and verify it does not escape the directory (path-traversal guard).

    Returns the resolved path, or None if the name is unsafe.
    """
    base = os.path.realpath(base_dir or DOWNLOAD_DIR)
    candidate = os.path.realpath(os.path.join(base, saved_name))
    if not candidate.startswith(base + os.sep) and candidate != base:
        return None
    return candidate


def delete_to_delete_files(engine, records):
    """
    For each record:
      - Delete the file from the filesystem.
      - If the deletion succeeds (or the file is already absent), set the
        corresponding DB record status to DELETED (keeping the record to
        prevent re-download).

    Returns a tuple (deleted_from_db, deleted_from_fs, skipped, errors).
    """
    deleted_from_db = 0
    deleted_from_fs = 0
    skipped = 0
    errors = []

    with engine.connect() as conn:
        for record in records:
            file_id, file_key, saved_name, channel_id, downloaded_at = record
            file_path = _safe_path(saved_name)

            if file_path is None:
                error_msg = f"[id={file_id}] Unsafe file path rejected: {saved_name!r}"
                logger.error(error_msg)
                errors.append(error_msg)
                continue

            fs_ok = False  # whether it is safe to remove the DB row

            try:
                os.remove(file_path)
                deleted_from_fs += 1
                logger.info(f"Deleted file from filesystem: {file_path}")
                fs_ok = True
            except FileNotFoundError:
                pw_path = _safe_path(saved_name, base_dir=PASSWORD_DUMP_DIR)
                if pw_path and os.path.exists(pw_path):
                    try:
                        os.remove(pw_path)
                        deleted_from_fs += 1
                        logger.info(f"Deleted file from password dumps: {pw_path}")
                        fs_ok = True
                    except Exception as e:
                        error_msg = f"[id={file_id}] Failed to delete from password dumps {pw_path}: {e}"
                        logger.error(error_msg)
                        errors.append(error_msg)
                else:
                    logger.warning(
                        f"[id={file_id}] File not found in downloads or password dumps, "
                        f"will still remove DB record: {file_path}"
                    )
                    skipped += 1
                    fs_ok = True  # file is already gone, safe to clean up the DB row
            except Exception as e:
                error_msg = f"[id={file_id}] Failed to delete {file_path}: {e}"
                logger.error(error_msg)
                errors.append(error_msg)

            if fs_ok:
                try:
                    with conn.begin():
                        conn.execute(
                            text(
                                "UPDATE downloaded_files SET status = 'DELETED' WHERE id = :id"
                            ),
                            {"id": file_id},
                        )
                    deleted_from_db += 1
                    logger.info(f"Marked DB record id={file_id} as DELETED")
                except Exception as e:
                    error_msg = f"[id={file_id}] Failed to update DB record: {e}"
                    logger.error(error_msg)
                    errors.append(error_msg)

    return deleted_from_db, deleted_from_fs, skipped, errors


def main():
    parser = argparse.ArgumentParser(
        description="Delete files marked TO_DELETE from filesystem and mark as DELETED in DB"
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )

    args = parser.parse_args()

    setup_logging()
    engine = get_db_engine()

    print("\n🔍 Fetching files with status TO_DELETE...\n")

    records = fetch_to_delete_files(engine)

    if not records:
        print("✅ No files found with status TO_DELETE.")
        return 0

    print(f"📊 Found {len(records)} file(s) to delete:\n")
    print("=" * 100)

    total_size = 0
    for idx, record in enumerate(records, 1):
        file_id, file_key, saved_name, channel_id, downloaded_at = record
        file_path = _safe_path(saved_name)
        if file_path is None:
            print(f"{idx}. ID: {file_id}  ⚠️  UNSAFE PATH REJECTED: {saved_name!r}")
            print("-" * 100)
            continue
        exists_download = os.path.exists(file_path)
        pw_path = _safe_path(saved_name, base_dir=PASSWORD_DUMP_DIR)
        exists_pw = bool(pw_path and os.path.exists(pw_path))

        if exists_download:
            try:
                total_size += os.path.getsize(file_path)
            except Exception:
                pass

        print(f"{idx}. ID: {file_id}")
        print(f"   File: {saved_name}")
        print(f"   Downloads: {file_path}  {'✓' if exists_download else '✗'}")
        print(
            f"   Password dumps: {pw_path or 'unsafe path'}  {
                '✓' if exists_pw else '✗'
            }"
        )
        print(f"   Channel: {channel_id}  |  Downloaded: {downloaded_at}")
        print("-" * 100)

    print(f"\n⚠️  Total files to delete: {len(records)}")

    if total_size > 0:
        size_mb = total_size / (1024 * 1024)
        print(f"💾 Total disk space to free: {size_mb:.2f} MB")

    # Confirmation
    if not args.yes:
        print(
            "\n❓ Do you want to delete these files from filesystem and mark as DELETED in DB?"
        )
        try:
            response = input("Type 'yes' to confirm: ").strip().lower()
        except EOFError:
            print(
                "\n❌ Deletion cancelled (no interactive input available; use --yes to skip prompt)."
            )
            return 0

        if response != "yes":
            print("\n❌ Deletion cancelled.")
            return 0

    # Perform deletion
    print("\n🗑️  Deleting files...")
    deleted_db, deleted_fs, skipped, errors = delete_to_delete_files(engine, records)

    # Summary
    print("\n" + "=" * 100)
    print("📋 Deletion Summary:")
    print(f"   Files deleted from filesystem:  {deleted_fs}")
    print(f"   Files not found (already gone): {skipped}")
    print(f"   Records marked as DELETED in DB: {deleted_db}")

    if errors:
        print(f"\n⚠️  Errors encountered ({len(errors)}):")
        for error in errors:
            print(f"   - {error}")
    else:
        print("\n✅ All operations completed successfully!")

    # Send confirmation email
    _send_deletion_email(records, deleted_db, deleted_fs, skipped, total_size, errors)

    return 0 if not errors else 1


def _send_deletion_email(records, deleted_db, deleted_fs, skipped, total_size, errors):
    body_parts = [
        f"Files to delete: {len(records)}",
        f"Deleted from filesystem: {deleted_fs}",
        f"Already gone (skipped): {skipped}",
        f"Records marked DELETED in DB: {deleted_db}",
    ]

    if total_size > 0:
        body_parts.append(f"Disk space freed: {total_size / (1024 * 1024):.2f} MB")

    body_parts.append("")
    body_parts.append("--- File list ---")
    for record in records:
        fid, _, fname, chid, dlat = record
        body_parts.append(f"ID {fid}: {fname} (channel {chid}, {dlat})")

    if errors:
        body_parts.append("")
        body_parts.append(f"--- Errors ({len(errors)}) ---")
        body_parts.extend(errors)

    body = "\n".join(body_parts)
    send_simple_email(
        "s.ansani@kirurusec.com",
        f"[DataLeak] Deletion Report – {len(records)} file(s) removed",
        body,
    )


if __name__ == "__main__":
    sys.exit(main())
