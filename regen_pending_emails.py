#!/usr/bin/env python3
"""
Regenerate subject and html_content for emails already queued in emails_to_send
who are still in PENDING/PENDING_SUSPECT but were stored with the old template.

Old rows contain the previous wording and masked passwords (e.g.
"- Host: X - Password: ********"). This script re-renders them with the current
"websites only, never passwords" template using the same functions as the
pipeline, without touching statuses or sent_emails.

Directories that no longer exist on disk (or whose re-analysis yields no hosts)
are left as-is: their queued emails keep the old text.

Usage:
    python regen_pending_emails.py --dry-run
    python regen_pending_emails.py --limit 100
"""

import argparse
import logging
from pathlib import Path

from sqlalchemy import text

import config
from analyze import analyze_directory, load_email_template
from utils import get_db_engine, setup_logging

logger = logging.getLogger(__name__)

DEFAULT_STATUSES = ("PENDING", "PENDING_SUSPECT")


def _status_clause(statuses):
    """Build a safe IN (...) clause plus its bound params for the status list."""
    placeholders = ", ".join(":s%d" % i for i in range(len(statuses)))
    params = {"s%d" % i: s for i, s in enumerate(statuses)}
    return "IN (%s)" % placeholders, params


def regen_pending_emails(dry_run: bool = False, limit: int | None = None, statuses=None):
    setup_logging()
    # Keep the re-analysis itself quiet: analyze_directory logs every file.
    logging.getLogger("analyze").setLevel(logging.WARNING)

    statuses = statuses or list(DEFAULT_STATUSES)
    engine = get_db_engine(config.DB_CONNECTION_STRING)
    clause, status_params = _status_clause(statuses)

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT analyzed_dir FROM emails_to_send "
                "WHERE status %s" % clause
            ),
            status_params,
        ).fetchall()
        total_pending = conn.execute(
            text("SELECT COUNT(*) FROM emails_to_send WHERE status %s" % clause),
            status_params,
        ).scalar() or 0

    dirs = [r.analyzed_dir for r in rows]
    if not dirs:
        logger.info("No pending emails found with status %s.", statuses)
        return

    if limit:
        dirs = dirs[:limit]

    logger.info(
        "Found %d pending email row(s) across %d directory(ies) with status %s.",
        total_pending,
        len(dirs),
        statuses,
    )

    dirs_ok = 0
    dirs_skipped = 0
    emails_regen = 0
    emails_unchanged = 0

    for analyzed_dir in dirs:
        path = Path(analyzed_dir)
        if not path.exists():
            logger.warning(
                "Directory not on disk (%s): leaving queued emails as-is",
                analyzed_dir,
            )
            dirs_skipped += 1
            continue

        try:
            _, _, hosts_with_password, _, _, _, _ = analyze_directory(str(path))
        except Exception as e:
            logger.error("Re-analysis failed for %s: %s", analyzed_dir, e)
            dirs_skipped += 1
            continue

        if not hosts_with_password:
            logger.warning(
                "Re-analysis found no hosts for %s: leaving queued emails as-is",
                analyzed_dir,
            )
            dirs_skipped += 1
            continue

        total_password_count = len(hosts_with_password)

        with engine.begin() as conn:
            pending = conn.execute(
                text(
                    "SELECT id, recipient_email, subject, html_content "
                    "FROM emails_to_send WHERE analyzed_dir = :dir "
                    "AND status %s ORDER BY id" % clause
                ),
                {"dir": analyzed_dir, **status_params},
            ).fetchall()

        for row in pending:
            new_subject, new_html = load_email_template(
                row.recipient_email, hosts_with_password, total_password_count
            )
            if new_html == row.html_content and new_subject == row.subject:
                emails_unchanged += 1
                continue

            old_has_pwd = "Password:" in row.html_content
            new_has_pwd = "Password:" in new_html
            logger.info(
                "[%s] id=%s recipient=%s subject: '%s' -> '%s' (old Password: %s, new Password: %s)",
                "DRY-RUN" if dry_run else "UPDATE",
                row.id,
                row.recipient_email,
                row.subject,
                new_subject,
                old_has_pwd,
                new_has_pwd,
            )
            if dry_run:
                emails_regen += 1
                continue

            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE emails_to_send SET subject = :subject,"
                        " html_content = :html WHERE id = :id"
                    ),
                    {"subject": new_subject, "html": new_html, "id": row.id},
                )
            emails_regen += 1

        dirs_ok += 1

    logger.info(
        "Done: directories ok=%d skipped=%d | emails regenerated=%d unchanged=%d",
        dirs_ok,
        dirs_skipped,
        emails_regen,
        emails_unchanged,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Regenerate subject/html_content of queued emails with the current template"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview changes without writing"
    )
    parser.add_argument("--limit", type=int, help="Max number of directories to process")
    parser.add_argument(
        "--status",
        nargs="*",
        default=list(DEFAULT_STATUSES),
        help="Email statuses to regenerate (default: PENDING PENDING_SUSPECT)",
    )
    args = parser.parse_args()
    regen_pending_emails(dry_run=args.dry_run, limit=args.limit, statuses=args.status)