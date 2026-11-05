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

import json
import os
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from telethon import events
from telethon.tl.types import PeerChannel

import config
from utils import (
    URI_QUEUE_MAP,
    LinkManager,
    TelegramBaseWorker,
    classify_uri,
    extract_links,
    get_db_engine,
    get_normalized_channel_id,
    get_publisher,
    is_joinable_telegram_link,
    load_active_channels,
    normalize_uri,
    setup_logging,
    uri_hash,
)

SESSION = os.getenv("TELETHON_LINK_WATCHER_SESSION", f"{config.SESSION}_link_watcher")

environment = get_db_engine()
link_manager = LinkManager(environment)

PENDING_RETRY_INTERVAL_SECONDS = 60


class StoreLinkResult(str, Enum):
    INSERTED = "inserted"
    DUPLICATE = "duplicate"
    ERROR = "error"


class DownloadLinksWorker(TelegramBaseWorker):
    """Estrae link dai messaggi Telegram e li pubblica sulle queue RabbitMQ."""

    def __init__(self):
        super().__init__(
            session=SESSION,
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            phone=config.API_PHONE,
            state_table="channel_state_gofile",
        )
        self.link_manager = link_manager
        self.publishers: dict = {}
        self._pending_retry_last_run: dict[str, datetime] = {}

    def get_publishers(self) -> dict:
        return {
            t: get_publisher(config.RABBIT_CONNECTION_STRING, q)
            for t, q in config.URI_QUEUE_MAP.items()
        }

    def get_extra_handlers(self) -> list:
        return [
            (
                lambda evt: self.process_message(evt.message, self.publishers),
                events.MessageEdited(chats=self.channels),
            )
        ]

    def store_link(
        self,
        uri: str,
        uri_sha256: str,
        uri_type: str,
        channel_id: str,
        channel_name: str,
        message_id: int,
        message_text: str,
    ) -> StoreLinkResult:
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
            self.logger.error("Failed to store link %s in DB (integrity): %s", uri, exc)
            return StoreLinkResult.ERROR
        except Exception as exc:
            self.logger.error("Failed to store link %s in DB: %s", uri, exc)
            return StoreLinkResult.ERROR

    def retry_pending_links(
        self, channel_id: str, limit: int = 100
    ) -> None:
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
            self.logger.error(
                "Failed to load pending links for channel %s: %s", channel_id, exc
            )
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
            queue_name = URI_QUEUE_MAP.get(row.uri_type)
            publisher = self.publishers.get(row.uri_type)
            if queue_name is None or publisher is None:
                self.link_manager.update_status(
                    row.uri, "FAILED", f"Unknown uri_type: {row.uri_type}"
                )
                continue

            if publisher.publish(payload):
                self.link_manager.update_status(row.uri, "PROCESSED")
                self.logger.info(
                    "Republished pending %s link for channel %s msg_id=%s to %s: %s",
                    row.uri_type,
                    channel_id,
                    row.message_id,
                    queue_name,
                    row.uri,
                )
            else:
                self.link_manager.update_status(
                    row.uri, "FAILED", f"Failed to publish to queue {queue_name}"
                )
                self.failed_publish(payload)

    def failed_publish(self, payload: dict) -> None:
        with open("failed_link_publish.json", "a", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.write("\n")

    async def process_message(self, msg, publishers: dict) -> None:
        cid = get_normalized_channel_id(msg)
        if cid is None:
            self.logger.debug("[#%s] Skipping message without channel id", msg.id)
            return

        ch_name = self.channel_names.get(cid, cid)
        last_id = int(self.state_manager.get(cid, 0))

        if msg.id <= last_id:
            self.logger.debug(
                "[#%s] Skipping already processed message on %s (last_id=%s)",
                msg.id,
                ch_name,
                last_id,
            )
            return

        now_utc = datetime.now(timezone.utc)
        last_retry = self._pending_retry_last_run.get(cid)
        if (
            last_retry is None
            or (now_utc - last_retry).total_seconds() >= PENDING_RETRY_INTERVAL_SECONDS
        ):
            self.retry_pending_links(cid)
            self._pending_retry_last_run[cid] = now_utc

        text_message = msg.message or msg.raw_text or ""
        links = extract_links(text_message)

        for link in links:
            uri_type = classify_uri(link)
            if uri_type == "TELEGRAM" and not is_joinable_telegram_link(link):
                self.logger.debug(
                    "[#%s] Skipping non-joinable Telegram link: %s", msg.id, link
                )
                continue

            link = normalize_uri(link)
            link_sha256 = uri_hash(link)
            store_result = self.store_link(
                link, link_sha256, uri_type, cid, ch_name, msg.id, text_message
            )
            if store_result == StoreLinkResult.DUPLICATE:
                existing_status = self.link_manager.get_status(link)
                if existing_status in (
                    "INSERTED",
                    "PROCESSED",
                    "ALREADY_SUBSCRIBED",
                    "JOIN_REQUESTED",
                    "LEFT",
                    "FAILED",
                ):
                    self.logger.debug(
                        "[#%s] Duplicate URI skipped (status: %s): %s",
                        msg.id,
                        existing_status,
                        link,
                    )
                    continue
                self.logger.info(
                    "[#%s] Retrying URI with status=%s: %s",
                    msg.id,
                    existing_status or "UNKNOWN",
                    link,
                )
            elif store_result == StoreLinkResult.ERROR:
                self.failed_publish(
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

            queue_name = URI_QUEUE_MAP.get(uri_type)
            publisher = publishers.get(uri_type)
            if queue_name is None or publisher is None:
                self.failed_publish(
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
                "detected_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
            if publisher.publish(payload):
                self.link_manager.update_status(link, "PROCESSED")
                self.logger.info(
                    "Published %s link from %s msg_id=%s to %s: %s",
                    uri_type,
                    ch_name,
                    msg.id,
                    queue_name,
                    link,
                )
            else:
                self.logger.error(
                    "Failed to publish %s link from %s msg_id=%s: %s",
                    uri_type,
                    ch_name,
                    msg.id,
                    link,
                )
                self.link_manager.update_status(
                    link,
                    "FAILED",
                    f"Failed to publish to queue {queue_name}",
                )
                self.failed_publish(payload)

        if not links:
            self.logger.debug("[#%s] No links found on %s", msg.id, ch_name)

        self.state_manager.save(cid, max(msg.id, last_id))

    async def catch_up(self, publishers: dict) -> None:
        for chat in self.channels:
            cid = str(chat)
            try:
                ent = await self.client.get_entity(PeerChannel(chat))
                name = getattr(ent, "title", getattr(ent, "username", cid))
                self.channel_names[cid] = name
            except Exception as exc:
                error_msg = f"Failed to get entity for channel {cid}: {exc}"
                self.logger.error(error_msg)
                continue
            last_id = int(self.state_manager.get(cid, 0))
            if last_id == 0:
                try:
                    msgs = await self.client.get_messages(chat, limit=1)
                    if msgs:
                        self.state_manager.save(cid, msgs[0].id)
                        self.logger.info("Seeded %s (ID: %s): %s", name, cid, msgs[0].id)
                    continue
                except Exception as exc:
                    self.logger.error(
                        "Failed to seed messages for %s (ID: %s): %s", name, cid, exc
                    )
                    continue
            self.logger.info("Catch-up %s (ID: %s) from %s", name, cid, last_id)

            try:
                async for message in self.client.iter_messages(
                    chat, min_id=last_id + 1, reverse=True
                ):
                    await self.process_message(message, publishers)
            except Exception as exc:
                error_msg = f"Error during catch-up for {name} (ID: {cid}): {exc}"
                self.logger.error(error_msg)
            self.logger.info("Done %s (ID: %s)", name, cid)


def main():
    setup_logging()

    worker = DownloadLinksWorker()
    worker.publishers = worker.get_publishers()

    worker.run(
        publisher=worker.publishers,
        channels_loader=lambda: load_active_channels(environment),
        extra_handlers=worker.get_extra_handlers(),
    )

    for pub in worker.publishers.values():
        pub.close()


if __name__ == "__main__":
    main()
