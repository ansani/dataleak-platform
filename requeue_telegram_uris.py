#!/usr/bin/env python3
"""
Script to requeue Telegram URIs from the database back into RabbitMQ.
Useful for recovering from crashes or retrying failed subscriptions.
"""

import json
import sys

from sqlalchemy import text

import config
from utils import get_db_engine, logger, setup_logging


def main():
    setup_logging()

    # Initialize DB
    engine = get_db_engine()

    # Query to find Telegram URIs that need processing
    # We include 'INSERTED', 'FAILED', and 'PROCESSED' to ensure we recover everything
    # that hasn't been explicitly marked as SUBSCRIBED.
    query = text(
        "SELECT uri, channel_id, channel_name, message_id, detected_at "
        "FROM discovered_links "
        "WHERE uri_type = 'TELEGRAM' AND status NOT IN ('NEW_SUBSCRIBED', 'ALREADY_SUBSCRIBED')"
    )

    try:
        with engine.begin() as conn:
            results = conn.execute(query).fetchall()
            if not results:
                logger.info("No Telegram URIs found in DB to requeue.")
                return

            logger.info("Found %d URIs to requeue.", len(results))

            # Initialize RabbitMQ
            import pika

            params = pika.URLParameters(config.RABBIT_CONNECTION_STRING)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.queue_declare(queue=config.URI_TELEGRAM_QUEUE, durable=True)

            count = 0
            for row in results:
                # Create the JSON payload expected by subscribe_channels.py
                payload = {
                    "channel_id": str(row.channel_id),
                    "channel_name": row.channel_name,
                    "message_id": row.message_id,
                    "uri": row.uri,
                    "uri_type": "TELEGRAM",
                    "detected_at": (
                        row.detected_at.strftime("%Y-%m-%d %H:%M:%S")
                        if row.detected_at
                        else None
                    ),
                }

                channel.basic_publish(
                    exchange="",
                    routing_key=config.URI_TELEGRAM_QUEUE,
                    body=json.dumps(payload),
                    properties=pika.BasicProperties(
                        delivery_mode=2,
                        content_type="application/json",  # Persistent
                    ),
                )
                count += 1

            connection.close()
            logger.info(
                "Successfully requeued %d URIs to '%s'.",
                count,
                config.URI_TELEGRAM_QUEUE,
            )

    except Exception as e:
        logger.error("Critical error during requeue process: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
