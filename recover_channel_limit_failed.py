#!/usr/bin/env python3
"""
Recover links marked as FAILED that were likely caused by channel limit reached.
Since error_description is NULL for existing failures, we identify by:
- Time window (when channel limit was hit)
- Valid channel URI patterns (not bots, not message links)
Usage: python recover_channel_limit_failed.py [--dry-run] [--since DATE] [--limit N]
"""

import argparse
import logging
import sys
from datetime import datetime

from sqlalchemy import text

from utils import get_db_engine, setup_logging

logger = logging.getLogger(__name__)


def is_valid_channel_uri(uri: str) -> bool:
    """Check if URI looks like a valid channel (not bot, not message link)."""
    if not uri or not uri.startswith("https://t.me/"):
        return False
    
    # Skip bot start links
    if "?start=" in uri:
        return False
    
    # Skip obvious bot usernames
    if any(x in uri.lower() for x in ["/bot", "bot?", "bot&", "_bot", "bot_"]):
        return False
    
    # Skip message links (t.me/channel/123)
    from urllib.parse import urlparse
    parsed = urlparse(uri)
    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) > 1 and not any(x in uri for x in ("joinchat/", "/+")):
        return False
    
    return True


def recover_channel_limit_failed(engine, since: datetime = None, limit: int = None, dry_run: bool = True):
    """
    Find FAILED links that look like valid channels and requeue them as INSERTED.
    """
    with engine.begin() as conn:
        # Build query
        conditions = ["status = 'FAILED'"]
        params = {}
        
        if since:
            conditions.append("detected_at >= :since")
            params["since"] = since
        
        where_clause = " AND ".join(conditions)
        
        query = text(f"""
            SELECT id, uri, uri_hash, detected_at
            FROM discovered_links
            WHERE {where_clause}
            ORDER BY detected_at ASC
        """)
        if limit:
            query = text(str(query) + f" LIMIT {limit}")
        
        rows = conn.execute(query, params).fetchall()
        
        if not rows:
            logger.info("No FAILED links found to recover")
            return 0
        
        # Filter for valid channel URIs
        valid_rows = [r for r in rows if is_valid_channel_uri(r.uri)]
        skipped = len(rows) - len(valid_rows)
        
        if skipped:
            logger.info(f"Skipped {skipped} invalid/bot/message links")
        
        if not valid_rows:
            logger.info("No valid channel links to recover")
            return 0
        
        logger.info(f"Found {len(valid_rows)} valid channel links to recover")
        
        if dry_run:
            for row in valid_rows:
                logger.info(f"[DRY RUN] Would recover: {row.uri} (id={row.id}, detected={row.detected_at})")
            return len(valid_rows)
        
        # Update status back to INSERTED for reprocessing
        update_query = text("""
            UPDATE discovered_links
            SET status = 'INSERTED',
                error_description = CONCAT('[RECOVERED FROM CHANNEL LIMIT] ', COALESCE(error_description, '')),
                updated_at = NOW()
            WHERE uri_hash = :uri_hash
        """)
        
        recovered = 0
        for row in valid_rows:
            try:
                conn.execute(update_query, {"uri_hash": row.uri_hash})
                logger.info(f"Recovered: {row.uri} (id={row.id}, detected={row.detected_at})")
                recovered += 1
            except Exception as e:
                logger.error(f"Failed to recover {row.uri}: {e}")
        
        logger.info(f"Successfully recovered {recovered} links")
        return recovered


def main():
    parser = argparse.ArgumentParser(description="Recover FAILED links likely caused by channel limit")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be recovered without changes")
    parser.add_argument("--since", type=str, help="Only recover links detected since DATE (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, help="Maximum number of links to recover")
    args = parser.parse_args()
    
    setup_logging()
    engine = get_db_engine()
    
    since = None
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            print("Invalid date format. Use YYYY-MM-DD")
            return 1
    
    recovered = recover_channel_limit_failed(engine, since=since, limit=args.limit, dry_run=args.dry_run)
    
    action = "Would recover" if args.dry_run else "Recovered"
    print(f"{action} {recovered} link(s)")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())