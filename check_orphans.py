#!/usr/bin/env python3
"""
Check for orphaned files and database records.

Two kinds of orphans:
1. DB orphans: records in downloaded_files whose saved_file_name does not exist
   in DOWNLOAD_DIR or PASSWORD_DUMP_DIR.
2. Filesystem orphans: files in DOWNLOAD_DIR or PASSWORD_DUMP_DIR that have no
   corresponding record in downloaded_files.

Usage: python check_orphans.py [-y]
"""

import argparse
import logging
import os
import sys

from sqlalchemy import text

from config import DOWNLOAD_DIR, PASSWORD_DUMP_DIR
from utils import get_db_engine, setup_logging

logger = logging.getLogger(__name__)


def _safe_path(saved_name, base_dir):
    """Path-traversal guard: return realpath or None if unsafe."""
    base = os.path.realpath(base_dir)
    candidate = os.path.realpath(os.path.join(base, saved_name))
    if not candidate.startswith(base + os.sep) and candidate != base:
        return None
    return candidate


def _collect_fs_files(directory):
    """Return set of non-.part filenames in directory (non-recursive)."""
    files = set()
    if not os.path.isdir(directory):
        return files
    for entry in os.listdir(directory):
        full = os.path.join(directory, entry)
        if os.path.isfile(full) and not entry.endswith(".part"):
            files.add(entry)
    return files


def find_db_orphans(engine):
    """Find DB records whose saved_file_name doesn't exist on disk."""
    query = text("""
        SELECT id, saved_file_name, status, file_key
        FROM downloaded_files
        ORDER BY id
    """)
    with engine.begin() as conn:
        rows = conn.execute(query).fetchall()

    orphans = []
    for row in rows:
        file_id, saved_name, status, file_key = row
        dl_path = _safe_path(saved_name, DOWNLOAD_DIR)
        pw_path = _safe_path(saved_name, PASSWORD_DUMP_DIR)
        dl_exists = bool(dl_path and os.path.exists(dl_path))
        pw_exists = bool(pw_path and os.path.exists(pw_path))
        if not dl_exists and not pw_exists:
            orphans.append(
                {
                    "id": file_id,
                    "saved_file_name": saved_name,
                    "status": status,
                    "file_key": file_key,
                }
            )
    return orphans


def find_fs_orphans(directory, db_filenames, label):
    """Find files in directory that have no matching DB record."""
    orphans = []
    for fname in sorted(_collect_fs_files(directory)):
        if fname not in db_filenames:
            path = _safe_path(fname, directory)
            if path:
                try:
                    size = os.path.getsize(path)
                except Exception:
                    size = 0
                orphans.append(
                    {"name": fname, "path": path, "size": size, "location": label}
                )
    return orphans


def delete_db_records(engine, records):
    """Delete orphaned DB records. Returns (deleted_count, error_list)."""
    deleted = 0
    errors = []
    with engine.connect() as conn:
        for rec in records:
            try:
                with conn.begin():
                    conn.execute(
                        text("DELETE FROM downloaded_files WHERE id = :id"),
                        {"id": rec["id"]},
                    )
                deleted += 1
                logger.info(
                    "Deleted DB record id=%d (%s)", rec["id"], rec["saved_file_name"]
                )
            except Exception as e:
                msg = f"Failed to delete DB record id={rec['id']}: {e}"
                logger.error(msg)
                errors.append(msg)
    return deleted, errors


def delete_fs_files(orphans):
    """Delete orphaned files from filesystem. Returns (deleted_count, error_list)."""
    deleted = 0
    errors = []
    for orphan in orphans:
        try:
            os.remove(orphan["path"])
            deleted += 1
            logger.info("Deleted file: %s", orphan["path"])
        except Exception as e:
            msg = f"Failed to delete {orphan['path']}: {e}"
            logger.error(msg)
            errors.append(msg)
    return deleted, errors


def main():
    parser = argparse.ArgumentParser(
        description="Check for orphaned files and DB records"
    )
    parser.add_argument(
        "-y", "--yes", action="store_true", help="Skip confirmation prompt"
    )
    args = parser.parse_args()

    setup_logging()
    engine = get_db_engine()

    print("\n🔍 Checking for orphans...\n")

    # Collect DB filenames for filesystem comparison
    query = text("SELECT saved_file_name FROM downloaded_files")
    with engine.begin() as conn:
        rows = conn.execute(query).fetchall()
    db_filenames = {row[0] for row in rows}
    print(f"📊 Records in DB: {len(db_filenames)}")

    # Find DB orphans (record exists, file missing on disk)
    db_orphans = find_db_orphans(engine)
    print(f"📊 DB orphans (record exists but file missing): {len(db_orphans)}")

    # Find filesystem orphans (file exists, no DB record)
    fs_orphans_dl = find_fs_orphans(DOWNLOAD_DIR, db_filenames, DOWNLOAD_DIR)
    fs_orphans_pw = find_fs_orphans(PASSWORD_DUMP_DIR, db_filenames, PASSWORD_DUMP_DIR)
    fs_orphans = fs_orphans_dl + fs_orphans_pw
    print(f"📊 Filesystem orphans (file exists but no DB record): {len(fs_orphans)}")

    if not db_orphans and not fs_orphans:
        print("\n✅ No orphans found.")
        return 0

    # Show details
    print("\n" + "=" * 100)

    if db_orphans:
        print(f"\n🗄️  DB orphans ({len(db_orphans)}):")
        print("-" * 100)
        for rec in db_orphans:
            print(
                f"   ID: {rec['id']:>8}  Status: {rec['status']:<20}  File: {
                    rec['saved_file_name']
                }"
            )

    if fs_orphans:
        total_fs_size = sum(o["size"] for o in fs_orphans)
        print(f"\n📁 Filesystem orphans ({len(fs_orphans)}):")
        print("-" * 100)
        for orphan in fs_orphans:
            size_str = f"{orphan['size'] / 1024:.1f} KB" if orphan["size"] > 0 else "?"
            print(f"   [{orphan['location']}] {size_str:>10}  {orphan['name']}")
        if total_fs_size > 0:
            print(
                f"\n💾 Total filesystem space: {total_fs_size / (1024 * 1024):.2f} MB"
            )

    # Confirmation
    want_db = bool(db_orphans)
    want_fs = bool(fs_orphans)

    if not args.yes:
        choices = []
        if want_db:
            choices.append("db")
        if want_fs:
            choices.append("fs")
        choices_str = " or ".join(choices)
        prompt = f"Type '{choices_str}' or 'all' to confirm: "
        try:
            response = input(f"\n❓ {prompt}").strip().lower()
        except EOFError:
            print("\n❌ Operation cancelled.")
            return 0

        allowed = set(choices + ["all"])
        if response not in allowed:
            print("\n❌ Operation cancelled.")
            return 0

        do_db = response in ("db", "all") and want_db
        do_fs = response in ("fs", "all") and want_fs
    else:
        do_db = want_db
        do_fs = want_fs

    # Execute deletions
    if do_db:
        print(f"\n🗑️  Deleting {len(db_orphans)} DB orphans...")
        db_deleted, db_errors = delete_db_records(engine, db_orphans)
        print(f"   Deleted {db_deleted} DB records.")
        for e in db_errors:
            print(f"   ⚠️  {e}")

    if do_fs:
        print(f"\n🗑️  Deleting {len(fs_orphans)} filesystem orphans...")
        fs_deleted, fs_errors = delete_fs_files(fs_orphans)
        print(f"   Deleted {fs_deleted} files.")
        for e in fs_errors:
            print(f"   ⚠️  {e}")

    print("\n✅ Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
