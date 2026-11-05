import asyncio
import time

from sqlalchemy import text
from telethon import TelegramClient, errors, events
from telethon.tl.types import Document

from config import (
    API_HASH,
    API_ID,
    API_PHONE,
    RABBIT_CONNECTION_STRING,
    SESSION,
    UNIQUES_QUEUE,
)
from utils import (
    IGNORED_EXTENSIONS_SET,
    DownloadManager,
    StateManager,
    cleanup_stale_downloading,
    download_and_register_document,
    get_db_engine,
    get_publisher,
    load_active_channels,
    logger,
    setup_logging,
)

# SQL statements removed in favor of DownloadManager

environment = get_db_engine()
download_manager = DownloadManager(environment)
state_manager = StateManager(environment, "channel_state", "channel_id", "last_id")

# Load channels from database
channels = load_active_channels(environment)

client = TelegramClient(SESSION, API_ID, API_HASH)
channel_names = {}


def disable_channel(channel_id: str, error_message: str):
    """Disable a channel in the database when it encounters errors."""
    try:
        with environment.begin() as conn:
            conn.execute(
                text(
                    "UPDATE channel_list SET status = 'DISABLED', date_removed = NOW() "
                    "WHERE channel_id = :channel_id"
                ),
                {"channel_id": channel_id},
            )
            logger.warning(
                "Disabled channel %s due to error: %s", channel_id, error_message
            )
    except Exception as e:
        logger.error(f"Failed to disable channel {channel_id} in database: {e}")


async def process_message(msg, publisher):  # Use RabbitMQPublisher
    cid = str(msg.chat_id)
    ch_name = channel_names.get(cid, cid)
    logger.debug("Processing msg_id=%s on %s (ID: %s)", msg.id, ch_name, cid)

    # Skip messages already processed
    last_id = state_manager.get(cid, 0)
    if msg.id <= last_id:
        logger.debug(
            "[#%s] Skipping already processed message (last_id=%s) on %s (ID: %s)",
            msg.id,
            last_id,
            ch_name,
            cid,
        )
        return

    if not msg.document or not isinstance(msg.document, Document):
        state_manager.save(cid, max(msg.id, last_id))
        return

    # Use shared download logic
    success = await download_and_register_document(
        client=client,
        msg=msg,
        publisher=publisher,
        download_manager=download_manager,
        cid=cid,
        ch_name=ch_name,
        ignored_extensions_set=IGNORED_EXTENSIONS_SET,
        on_skip_callback=lambda: state_manager.save(cid, max(msg.id, last_id)),
    )

    if success:
        state_manager.save(cid, max(msg.id, last_id))


async def catch_up(publisher):  # Use RabbitMQPublisher
    for chat in channels:
        cid = str(chat)
        try:
            ent = await client.get_entity(
                chat
            )  # Use chat ID directly instead of PeerChannel
            nm = getattr(ent, "title", getattr(ent, "username", cid))
            channel_names[cid] = nm
        except Exception as e:
            error_msg = f"Failed to get entity for channel {cid}: {e!s}"
            logger.error(error_msg)
            # Disable the channel in the database
            disable_channel(cid, error_msg)
            nm = cid
            continue  # Skip processing this channel
        last = state_manager.get(cid, 0)
        if last == 0:
            try:
                msgs = await client.get_messages(chat, limit=1)
                if msgs:
                    state_manager.save(cid, msgs[0].id)
                    logger.info("Seeded %s (ID: %s): %s", nm, cid, msgs[0].id)
                continue
            except Exception as e:
                logger.error(
                    "Failed to seed messages for %s (ID: %s): %s", nm, cid, str(e)
                )
                continue
        logger.info("Catch-up %s (ID: %s) from %s", nm, cid, last)
        try:
            async for m in client.iter_messages(chat, min_id=last + 1, reverse=True):
                await process_message(m, publisher)  # Pass publisher
        except Exception as e:
            error_msg = f"Error during catch-up for {nm} (ID: {cid}): {e!s}"
            logger.error(error_msg)
            # Disable the channel if it's a channel-specific error
            if (
                "channel" in str(e).lower()
                or "forbidden" in str(e).lower()
                or "not found" in str(e).lower()
                or "unhashable" in str(e).lower()
            ):
                disable_channel(cid, error_msg)
        logger.info("Done %s (ID: %s)", nm, cid)


def main():
    setup_logging()

    # Cleanup stale DOWNLOADING records (older than 24 hours)
    cleanup_stale_downloading(download_manager)

    # Init RabbitMQ
    publisher = get_publisher(RABBIT_CONNECTION_STRING, UNIQUES_QUEUE)

    # Register handler explicitly so we can remove/update it safely later.
    client_message_handler = None

    def register_message_handler(chats_list):
        nonlocal client_message_handler
        # Remove existing handler if present
        try:
            if client_message_handler is not None:
                client.remove_event_handler(client_message_handler)
        except Exception:
            # best-effort removal
            pass

        # Define and add new handler
        async def _handler(evt):
            await process_message(evt.message, publisher)

        client_message_handler = client.add_event_handler(
            _handler, events.NewMessage(chats=chats_list)
        )

    try:
        client.start(phone=API_PHONE)
    except errors.ApiIdInvalidError:
        logger.error("Invalid API credentials.")
        return

    logger.info("Connected. Starting...")

    # Register initial handler
    register_message_handler(channels)

    # Reload channels periodically (every hour)
    async def reload_channels_periodically():
        while True:
            await asyncio.sleep(3600)  # Wait 1 hour
            global channels
            new_channels = load_active_channels(environment)
            if new_channels != channels:
                logger.info("Reloaded channels: %d active channels", len(new_channels))
                channels = new_channels
                # Update the event handler with new channels
                register_message_handler(channels)

    client.loop.create_task(catch_up(publisher))  # Pass publisher
    client.loop.create_task(
        reload_channels_periodically()
    )  # Reload channels periodically
    # Run the client until disconnected, with a retry loop for connection errors
    while True:
        try:
            client.run_until_disconnected()
            break  # Exit loop if disconnected normally
        except (ConnectionError, errors.ConnectionError) as e:
            logger.error(
                "Connection to Telegram failed: %s. Retrying in 60 seconds...", e
            )
            time.sleep(60)
        except Exception as e:
            logger.error("Unexpected error in run_until_disconnected: %s", e)
            # For other unexpected errors, we might want to crash or retry
            # Let's retry most errors but with a longer delay
            time.sleep(30)


if __name__ == "__main__":
    main()
