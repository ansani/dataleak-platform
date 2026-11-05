#!/usr/bin/env python3
"""
Clean up discovered_links TELEGRAM URIs and republish valid ones.

Scans all TELEGRAM links, normalizes URIs, filters out non-joinable links
(message links, bots), merges duplicates, purges the RabbitMQ queue, and
republishes only valid INSERTED links.

Usage: python cleanup_telegram_links.py
"""

import json
import logging
from collections import defaultdict

import pika
from sqlalchemy import text

import config
from utils import (
    get_db_engine,
    get_rabbit_connection,
    is_joinable_telegram_link,
    normalize_uri,
    setup_logging,
    uri_hash,
)

logger = logging.getLogger(__name__)


def main():
    setup_logging()
    engine = get_db_engine()

    print("\n=== Step 1: Scan TELEGRAM links ===\n")

    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT id, uri, uri_hash, channel_id, channel_name, message_id, status,
                       error_description
                FROM discovered_links
                WHERE uri_type = 'TELEGRAM'
                ORDER BY id
            """)
        ).fetchall()

    print(f"Found {len(rows)} TELEGRAM links.\n")

    non_joinable = []
    to_normalize = []
    unchanged_valid = []

    seen_by_normalized: dict[str, list[dict]] = defaultdict(list)

    for row in rows:
        rec = {
            "id": row.id,
            "uri": row.uri,
            "uri_hash": row.uri_hash,
            "channel_id": row.channel_id,
            "channel_name": row.channel_name,
            "message_id": row.message_id,
            "status": row.status,
            "error_description": row.error_description,
        }

        if not is_joinable_telegram_link(rec["uri"]):
            non_joinable.append(rec)
            continue

        normalized = normalize_uri(rec["uri"])
        new_hash = uri_hash(normalized)

        if new_hash != rec["uri_hash"]:
            rec["new_uri"] = normalized
            rec["new_hash"] = new_hash
            to_normalize.append(rec)
            seen_by_normalized[new_hash].append(rec)
        else:
            seen_by_normalized[rec["uri_hash"]].append(rec)
            if normalized != rec["uri"]:
                rec["new_uri"] = normalized
                rec["new_hash"] = new_hash
                to_normalize.append(rec)
            else:
                unchanged_valid.append(rec)

    # Detect duplicates within normalized group
    normalize_ids = {r["id"] for r in to_normalize}
    merge_groups = {
        h: [r for r in recs if r["id"] not in normalize_ids]
        for h, recs in seen_by_normalized.items()
        if len(recs) > 1
    }
    merge_groups = {h: recs for h, recs in merge_groups.items() if len(recs) > 1}

    print(f"  Non-joinable (message links, bots): {len(non_joinable)}")
    print(f"  URI needs normalization:              {len(to_normalize)}")
    print(f"  Duplicate groups detected:            {len(merge_groups)}")
    print(f"  Unchanged valid:                      {len(unchanged_valid)}\n")

    # --- Step 2: Apply fixes ---

    print("=== Step 2: Update DB ===\n")

    with engine.begin() as conn:
        # 2a. Mark non-joinable as FAILED
        for rec in non_joinable:
            conn.execute(
                text("""
                    UPDATE discovered_links
                    SET status = 'FAILED',
                        error_description = 'Invalid URI (message link / bot)',
                        updated_at = NOW()
                    WHERE id = :id
                """),
                {"id": rec["id"]},
            )
        print(f"  Marked {len(non_joinable)} non-joinable links as FAILED.")

        # 2b. Normalize URIs (skip if new hash collides with existing record)
        normalized_ok = 0
        normalized_dup = 0
        for rec in to_normalize:
            existing = conn.execute(
                text(
                    "SELECT id FROM discovered_links WHERE uri_hash = :hash AND id != :id LIMIT 1"
                ),
                {"hash": rec["new_hash"], "id": rec["id"]},
            ).fetchone()
            if existing:
                conn.execute(
                    text("""
                        UPDATE discovered_links
                        SET status = 'FAILED',
                            error_description = CONCAT('Normalized duplicate of id=', :existing_id),
                            updated_at = NOW()
                        WHERE id = :id
                    """),
                    {"existing_id": existing[0], "id": rec["id"]},
                )
                normalized_dup += 1
            else:
                conn.execute(
                    text("""
                        UPDATE discovered_links
                        SET uri = :new_uri,
                            uri_hash = :new_hash,
                            updated_at = NOW()
                        WHERE id = :id
                    """),
                    {
                        "new_uri": rec["new_uri"],
                        "new_hash": rec["new_hash"],
                        "id": rec["id"],
                    },
                )
                normalized_ok += 1
        print(
            f"  Normalized {normalized_ok} URIs ({normalized_dup} merged as duplicates)."
        )

        # 2c. Merge duplicates: keep first (lowest id), mark others as FAILED
        merged_count = 0
        for _h, recs in merge_groups.items():
            recs.sort(key=lambda r: r["id"])
            keeper = recs[0]
            for dup in recs[1:]:
                conn.execute(
                    text("""
                        UPDATE discovered_links
                        SET status = 'FAILED',
                            error_description = CONCAT(
                                'Duplicate of id=', :keeper_id,
                                ' (', :keeper_uri, ')'
                            ),
                            updated_at = NOW()
                        WHERE id = :id
                    """),
                    {
                        "keeper_id": keeper["id"],
                        "keeper_uri": keeper["uri"],
                        "id": dup["id"],
                    },
                )
                merged_count += 1
        print(f"  Merged {merged_count} duplicate links.\n")

    # --- Step 3: Purge RabbitMQ queue and republish valid links ---
    print("=== Step 3: RabbitMQ queue ===\n")

    try:
        conn_rmq = get_rabbit_connection(config.RABBIT_CONNECTION_STRING, heartbeat=60)
        channel = conn_rmq.channel()
        queue_name = config.URI_TELEGRAM_QUEUE
    except Exception as e:
        print(f"  Failed to connect to RabbitMQ: {e}")
        print("  Skipping queue purge and republish.\n")
        return

    # 3a. Purge queue
    try:
        channel.queue_purge(queue=queue_name)
        print(f"  Purged queue '{queue_name}'.")
    except Exception as e:
        print(f"  Failed to purge queue: {e}")

    # 3b. Republish valid INSERTED links
    with engine.begin() as conn:
        inserteds = conn.execute(
            text("""
                SELECT id, uri, uri_hash, channel_id, channel_name, message_id
                FROM discovered_links
                WHERE uri_type = 'TELEGRAM'
                  AND status = 'INSERTED'
                ORDER BY id
            """)
        ).fetchall()

    published = 0
    for row in inserteds:
        payload = {
            "channel_id": row.channel_id,
            "channel_name": row.channel_name,
            "message_id": row.message_id,
            "uri": row.uri,
            "uri_type": "TELEGRAM",
            "detected_at": "cleanup-republish",
        }
        try:
            channel.basic_publish(
                exchange="",
                routing_key=queue_name,
                body=json.dumps(payload).encode("utf-8"),
                properties=pika.BasicProperties(
                    delivery_mode=2, content_type="application/json"
                ),
            )
            published += 1
        except Exception as e:
            logger.error("Failed to publish id=%d: %s", row.id, e)

    conn_rmq.close()
    print(f"  Republished {published} INSERTED links to '{queue_name}'.\n")

    # --- Summary ---
    print("=== Summary ===\n")
    print(f"  Total TELEGRAM links scanned:  {len(rows)}")
    print(f"  Marked non-joinable (FAILED):  {len(non_joinable)}")
    print(f"  URI normalized:                {normalized_ok}")
    print(f"  Normalized merged as dups:     {normalized_dup}")
    print(f"  Merged as duplicates:          {merged_count}")
    print(f"  Republished to queue:          {published}")
    print()


if __name__ == "__main__":
    main()
