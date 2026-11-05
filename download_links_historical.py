#!/usr/bin/env python3
"""
Historical link downloader.
Iterates through active Telegram channels for a specified number of days
and publishes all discovered links to RabbitMQ queues.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from telethon import TelegramClient
from telethon.tl.types import PeerChannel

from config import (
    API_HASH,
    API_ID,
    API_PHONE,
    RABBIT_CONNECTION_STRING,
)
from utils import (
    URI_QUEUE_MAP,
    LinkManager,
    classify_uri,
    extract_links,
    get_db_engine,
    get_normalized_channel_id,
    get_publisher,
    load_active_channels,
    logger,
    setup_logging,
    uri_hash,
)

_QUEUE_FOR_TYPE = URI_QUEUE_MAP

environment = get_db_engine()
link_manager = LinkManager(environment)
client = TelegramClient("links_historical", API_ID, API_HASH)
channel_names = {}


def store_link(
    uri: str,
    uri_sha256: str,
    uri_type: str,
    channel_id: str,
    channel_name: str,
    message_id: int,
    message_text: str,
) -> bool:
    """Insert link into discovered_links. Returns True if inserted or already exists (no error)."""
    try:
        with environment.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO discovered_links
                        (uri, uri_hash, uri_type, channel_id, channel_name, message_id, message_text, status)
                    VALUES
                        (:uri, :uri_hash, :uri_type, :channel_id, :channel_name, :message_id, :message_text, 'INSERTED')
                """),
                {
                    "uri": uri,
                    "uri_hash": uri_sha256,
                    "uri_type": uri_type,
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "message_id": message_id,
                    "message_text": message_text,
                },
            )
            return True
    except Exception as e:
        # Handle duplicate entry silently as it's expected in historical runs
        if "Duplicate entry" in str(e):
            return True
        logger.error("Failed to store link %s in DB: %s", uri, e)
        return False


async def process_message(msg, publishers):
    cid = get_normalized_channel_id(msg)
    if cid is None:
        return

    ch_name = channel_names.get(cid, cid)
    text_message = msg.message or msg.raw_text or ""
    links = extract_links(text_message)

    for link in links:
        uri_type = classify_uri(link)
        link_sha256 = uri_hash(link)

        if store_link(link, link_sha256, uri_type, cid, ch_name, msg.id, text_message):
            # Check if it's already processed or currently in queue to avoid
            # spamming queues
            status = link_manager.get_status(link)
            if status in (
                "INSERTED",
                "PROCESSED",
                "ALREADY_SUBSCRIBED",
                "JOIN_REQUESTED",
            ):
                continue

            publisher = publishers.get(uri_type)
            if not publisher:
                continue

            payload = {
                "channel_id": cid,
                "channel_name": ch_name,
                "message_id": msg.id,
                "uri": link,
                "uri_type": uri_type,
                "detected_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            }
            if publisher.publish(payload):
                link_manager.update_status(link, "PROCESSED")
                logger.info("Published %s link from %s: %s", uri_type, ch_name, link)


async def process_channel(chat, start_date, publishers):
    cid = str(chat)
    try:
        ent = await client.get_entity(PeerChannel(int(chat)))
        nm = getattr(ent, "title", getattr(ent, "username", cid))
        channel_names[cid] = nm
    except Exception as e:
        logger.error("Failed to get entity for channel %s: %s", cid, e)
        return

    logger.info(
        "Processing historical links for %s (ID: %s) since %s", nm, cid, start_date
    )

    try:
        async for msg in client.iter_messages(chat):
            if msg.date.astimezone(timezone.utc) < start_date:
                break
            await process_message(msg, publishers)
    except Exception as e:
        logger.error("Error iterating messages for %s: %s", nm, e)
    logger.info("Done %s", nm)


async def main():
    parser = argparse.ArgumentParser(
        description="Download historical links from Telegram channels."
    )
    parser.add_argument(
        "--days", type=int, default=30, help="Number of days to look back (default: 30)"
    )
    args = parser.parse_args()

    setup_logging()

    publishers = {
        t: get_publisher(RABBIT_CONNECTION_STRING, q) for t, q in URI_QUEUE_MAP.items()
    }

    try:
        await client.start(phone=API_PHONE)
    except Exception as e:
        logger.error("Client start failed: %s", e)
        return

    channels = load_active_channels(environment)
    start_date = datetime.now(timezone.utc) - timedelta(days=args.days)

    for chat in channels:
        await process_channel(chat, start_date, publishers)

    await client.disconnect()
    for pub in publishers.values():
        pub.close()


if __name__ == "__main__":
    asyncio.run(main())
