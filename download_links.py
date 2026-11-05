#!/usr/bin/env python3
"""Watch Telegram messages and publish discovered links to RabbitMQ queues.

Each URI found in message text is stored once in `discovered_links` (unique URI
constraint, status INSERTED). Links are then routed to a specific queue:
  - GoFile links  (gofile.io)            → URI_GOFILE queue
  - Telegram links (t.me / telegram.me)  → URI_TELEGRAM queue
  - All other HTTP/HTTPS links           → URI_GENERIC queue

This script is intentionally separate from download.py: it scans every message
text (not only document attachments) and keeps its own cursor state so it can
run alongside the normal Telegram file downloader.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from telethon import TelegramClient, errors, events
from telethon.tl.types import PeerChannel

import config
from config import (
    API_HASH,
    API_ID,
    API_PHONE,
    RABBIT_CONNECTION_STRING,
)
from utils import (
    URI_QUEUE_MAP,
    LinkManager,
    RabbitMQPublisher,
    StateManager,
    classify_uri,
    extract_links,
    get_db_engine,
    get_normalized_channel_id,
    get_publisher,
    is_joinable_telegram_link,
    load_active_channels,
    logger,
    normalize_uri,
    setup_logging,
    uri_hash,
)

SESSION = os.getenv("TELETHON_LINK_WATCHER_SESSION", f"{config.SESSION}_link_watcher")


class StoreLinkResult(str, Enum):
    INSERTED = "inserted"
    DUPLICATE = "duplicate"
    ERROR = "error"


_QUEUE_FOR_TYPE = URI_QUEUE_MAP

environment = get_db_engine()


state_manager = StateManager(
    environment, "channel_state_gofile", "channel_id", "last_id"
)
link_manager = LinkManager(environment)
client = TelegramClient(SESSION, API_ID, API_HASH)
channel_names: dict[str, str] = {}
channels: list[int] = []
_pending_retry_last_run: dict[str, datetime] = {}
_PENDING_RETRY_INTERVAL_SECONDS = 60


def load_state() -> dict:
    # Now handled by StateManager
    return state_manager.load_all()


def save_state(channel_id: str, last_id: int) -> None:
    # Now handled by StateManager
    state_manager.save(channel_id, last_id)


def load_channels_from_db() -> list[int]:
    return load_active_channels(environment)


def store_link(
    uri: str,
    uri_sha256: str,
    uri_type: str,
    channel_id: str,
    channel_name: str,
    message_id: int,
    message_text: str,
) -> StoreLinkResult:
    """Insert *uri* into discovered_links with status INSERTED.

    Returns INSERTED if newly inserted, DUPLICATE if already present, ERROR on DB issues.
    """
    try:
        with environment.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO discovered_links
                        (uri, uri_hash, uri_type, channel_id, channel_name, message_id, message_text,
                         status)
                    VALUES
                        (:uri, :uri_hash, :uri_type, :channel_id, :channel_name, :message_id,
                         :message_text, 'INSERTED')
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
            return StoreLinkResult.INSERTED
    except IntegrityError as exc:
        orig = getattr(exc, "orig", None)
        mysql_error_code = None
        if orig and hasattr(orig, "args") and orig.args:
            mysql_error_code = orig.args[0]
            if isinstance(mysql_error_code, str) and mysql_error_code.isdigit():
                mysql_error_code = int(mysql_error_code)
        if mysql_error_code == 1062:
            return StoreLinkResult.DUPLICATE

        err_msg = str(orig or exc)
        if "Duplicate entry" in err_msg:
            return StoreLinkResult.DUPLICATE
        logger.error("Failed to store link %s in DB (integrity): %s", uri, exc)
        return StoreLinkResult.ERROR
    except Exception as exc:
        logger.error("Failed to store link %s in DB: %s", uri, exc)
        return StoreLinkResult.ERROR


def retry_pending_links(
    channel_id: str,
    publishers: dict[str, RabbitMQPublisher],
    limit: int = 100,
) -> None:
    """Retry publishing discovered links still marked INSERTED/FAILED for one channel."""
    try:
        with environment.begin() as conn:
            rows = conn.execute(
                text("""
                    SELECT uri, uri_hash, uri_type, channel_name, message_id
                    FROM discovered_links
                    WHERE channel_id = :channel_id
                      AND status = 'INSERTED'
                    ORDER BY detected_at ASC
                    LIMIT :limit
                """),
                {"channel_id": channel_id, "limit": limit},
            ).fetchall()
    except Exception as exc:
        logger.error("Failed to load pending links for channel %s: %s", channel_id, exc)
        return

    for row in rows:
        payload = {
            "channel_id": channel_id,
            "channel_name": row.channel_name,
            "message_id": row.message_id,
            "uri": row.uri,
            "uri_type": row.uri_type,
            "detected_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
        queue_name = _QUEUE_FOR_TYPE.get(row.uri_type)
        publisher = publishers.get(row.uri_type)
        if queue_name is None or publisher is None:
            link_manager.update_status(
                row.uri, "FAILED", f"Unknown uri_type: {row.uri_type}"
            )
            continue

        if publisher.publish(payload):
            link_manager.update_status(row.uri, "PROCESSED")
            logger.info(
                "Republished pending %s link for channel %s msg_id=%s to %s: %s",
                row.uri_type,
                channel_id,
                row.message_id,
                queue_name,
                row.uri,
            )
        else:
            link_manager.update_status(
                row.uri, "FAILED", f"Failed to publish to queue {queue_name}"
            )
            failed_publish(payload)


def failed_publish(payload: dict) -> None:
    with open("failed_link_publish.json", "a", encoding="utf-8") as handle:
        json.dump(payload, handle)
        handle.write("\n")


async def process_message(msg, publishers: dict[str, RabbitMQPublisher]) -> None:
    cid = get_normalized_channel_id(msg)
    if cid is None:
        logger.debug("[#%s] Skipping message without channel id", msg.id)
        return

    ch_name = channel_names.get(cid, cid)
    last_id = int(state_manager.get(cid, 0))

    if msg.id <= last_id:
        logger.debug(
            "[#%s] Skipping already processed message on %s (last_id=%s)",
            msg.id,
            ch_name,
            last_id,
        )
        return

    now_utc = datetime.now(timezone.utc)
    last_retry = _pending_retry_last_run.get(cid)
    if (
        last_retry is None
        or (now_utc - last_retry).total_seconds() >= _PENDING_RETRY_INTERVAL_SECONDS
    ):
        retry_pending_links(cid, publishers)
        _pending_retry_last_run[cid] = now_utc

    text_message = msg.message or msg.raw_text or ""
    links = extract_links(text_message)

    for link in links:
        uri_type = classify_uri(link)
        # Skip non-joinable Telegram links (message links, bots, etc.)
        if uri_type == "TELEGRAM" and not is_joinable_telegram_link(link):
            logger.debug("[#%s] Skipping non-joinable Telegram link: %s", msg.id, link)
            continue

        # Normalize URI before storing and publishing
        link = normalize_uri(link)
        link_sha256 = uri_hash(link)
        store_result = store_link(
            link, link_sha256, uri_type, cid, ch_name, msg.id, text_message
        )
        if store_result == StoreLinkResult.DUPLICATE:
            existing_status = link_manager.get_status(link)
            if existing_status in (
                "INSERTED",
                "PROCESSED",
                "ALREADY_SUBSCRIBED",
                "JOIN_REQUESTED",
                "LEFT",
                "FAILED",
            ):
                logger.debug(
                    "[#%s] Duplicate URI skipped (status: %s): %s",
                    msg.id,
                    existing_status,
                    link,
                )
                continue
            logger.info(
                "[#%s] Retrying URI with status=%s: %s",
                msg.id,
                existing_status or "UNKNOWN",
                link,
            )
        elif store_result == StoreLinkResult.ERROR:
            failed_publish(
                {
                    "channel_id": cid,
                    "channel_name": ch_name,
                    "message_id": msg.id,
                    "uri": link,
                    "uri_type": uri_type,
                    "error": "Failed to store link in DB",
                    "detected_at": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                }
            )
            continue

        queue_name = _QUEUE_FOR_TYPE.get(uri_type)
        publisher = publishers.get(uri_type)
        if queue_name is None or publisher is None:
            failed_publish(
                {
                    "channel_id": cid,
                    "channel_name": ch_name,
                    "message_id": msg.id,
                    "uri": link,
                    "uri_type": uri_type,
                    "error": f"Unknown uri_type: {uri_type}",
                    "detected_at": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                }
            )
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
            logger.info(
                "Published %s link from %s msg_id=%s to %s: %s",
                uri_type,
                ch_name,
                msg.id,
                queue_name,
                link,
            )
        else:
            logger.error(
                "Failed to publish %s link from %s msg_id=%s: %s",
                uri_type,
                ch_name,
                msg.id,
                link,
            )
            link_manager.update_status(
                link,
                "FAILED",
                f"Failed to publish to queue {queue_name}",
            )
            failed_publish(payload)

    if not links:
        logger.debug("[#%s] No links found on %s", msg.id, ch_name)

    state_manager.save(cid, max(msg.id, last_id))


async def catch_up(
    chat_ids: list[int], publishers: dict[str, RabbitMQPublisher]
) -> None:
    for chat in chat_ids:
        cid = str(chat)
        try:
            ent = await client.get_entity(PeerChannel(chat))
            name = getattr(ent, "title", getattr(ent, "username", cid))
            channel_names[cid] = name
        except Exception as exc:
            error_msg = f"Failed to get entity for channel {cid}: {exc}"
            logger.error(error_msg)
            continue
        last_id = int(state_manager.get(cid, 0))
        if last_id == 0:
            try:
                msgs = await client.get_messages(chat, limit=1)
                if msgs:
                    state_manager.save(cid, msgs[0].id)
                    logger.info("Seeded %s (ID: %s): %s", name, cid, msgs[0].id)
                continue
            except Exception as exc:
                logger.error(
                    "Failed to seed messages for %s (ID: %s): %s", name, cid, exc
                )
                continue
        logger.info("Catch-up %s (ID: %s) from %s", name, cid, last_id)

        try:
            async for message in client.iter_messages(
                chat, min_id=last_id + 1, reverse=True
            ):
                await process_message(message, publishers)
        except Exception as exc:
            error_msg = f"Error during catch-up for {name} (ID: {cid}): {exc}"
            logger.error(error_msg)
        logger.info("Done %s (ID: %s)", name, cid)


def main() -> None:
    global channels

    setup_logging()
    channels = load_channels_from_db()

    publishers: dict[str, RabbitMQPublisher] = {
        t: get_publisher(RABBIT_CONNECTION_STRING, q) for t, q in URI_QUEUE_MAP.items()
    }

    client_message_handler = None

    def register_message_handler(chats_list):
        nonlocal client_message_handler
        try:
            if client_message_handler is not None:
                client.remove_event_handler(client_message_handler)
        except Exception:
            pass

        async def _handler(evt):
            await process_message(evt.message, publishers)

        client_message_handler = client.add_event_handler(
            _handler, events.NewMessage(chats=chats_list)
        )

    try:
        client.start(phone=API_PHONE)
    except errors.ApiIdInvalidError:
        logger.error("Invalid API credentials.")
        for pub in publishers.values():
            pub.close()
        return

    logger.info("Connected. Watching every message for links.")
    register_message_handler(channels)

    # Add handler for edited messages to capture links added later
    client.add_event_handler(
        lambda evt: process_message(evt.message, publishers),
        events.MessageEdited(chats=channels),
    )

    async def reload_channels_periodically():
        global channels
        while True:
            await asyncio.sleep(3600)
            new_channels = load_channels_from_db()
            if new_channels != channels:
                # Identify which channels are actually new
                added_channels = list(set(new_channels) - set(channels))
                removed_channels = list(set(channels) - set(new_channels))

                logger.info(
                    "Reloaded channels: %d active channels. Added: %d, Removed: %d",
                    len(new_channels),
                    len(added_channels),
                    len(removed_channels),
                )

                channels = new_channels
                register_message_handler(channels)

                # Trigger catch-up only for new channels to avoid reprocessing
                if added_channels:
                    logger.info(
                        "Triggering catch-up for %d new channels...",
                        len(added_channels),
                    )
                    client.loop.create_task(catch_up(added_channels, publishers))

    try:
        client.loop.create_task(catch_up(channels, publishers))
        client.loop.create_task(reload_channels_periodically())
        client.run_until_disconnected()
    finally:
        for pub in publishers.values():
            pub.close()


if __name__ == "__main__":
    main()
