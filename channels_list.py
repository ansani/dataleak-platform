import asyncio
import logging

from sqlalchemy import text
from telethon.sync import TelegramClient

import config
from utils import get_db_engine, send_simple_email, setup_logging

# Create the client using config parameters
client = TelegramClient("CHANNEL_LIST", config.API_ID, config.API_HASH)


async def get_subscribed_channels():
    """
    Iterates through subscribed channels and syncs them with the database.
    Returns a summary report.
    """
    logger = logging.getLogger(__name__)

    # Get database engine
    engine = get_db_engine()

    # Start the client using config phone number
    await client.start(config.API_PHONE)

    logger.info("Syncing subscribed channels...")
    logger.info("-" * 50)

    # Count for statistics
    total_channels = 0
    added_channels = 0
    updated_channels = 0
    modified_channels_list = []

    # Get all dialogues
    async for dialog in client.iter_dialogs():
        # Check if it's a channel
        if hasattr(dialog.entity, "broadcast") and dialog.entity.broadcast:
            channel = dialog.entity
            channel_id = str(channel.id)
            channel_title = channel.title or "Unknown Channel"
            channel_link = (
                f"https://t.me/{channel.username}"
                if getattr(channel, "username", None)
                else "No public link"
            )

            total_channels += 1

            # Insert or update channel in database
            try:
                with engine.begin() as conn:
                    # Use INSERT ... ON DUPLICATE KEY UPDATE to handle duplicates
                    conn.execute(
                        text("""
                        INSERT INTO channel_list (channel_name, channel_id, status)
                        VALUES (:channel_name, :channel_id, 'ACTIVE')
                        ON DUPLICATE KEY UPDATE
                            channel_name = VALUES(channel_name),
                            updated_at = IF(channel_name != VALUES(channel_name), NOW(), updated_at)
                        """),
                        {"channel_name": channel_title, "channel_id": channel_id},
                    )

                    # Check if it was an insert or update
                    result = conn.execute(
                        text("SELECT ROW_COUNT() as affected_rows")
                    ).fetchone()

                    # ROW_COUNT: 1=INSERT, 2=UPDATE (name changed), 0=no change
                    if result.affected_rows == 1:
                        logger.debug(
                            "Added: %s (ID: %s) - %s",
                            channel_title,
                            channel_id,
                            channel_link,
                        )
                        added_channels += 1
                        modified_channels_list.append(
                            f"[NEW] {channel_title} ({channel_id}) - {channel_link}"
                        )
                    elif result.affected_rows == 2:
                        logger.debug("Updated: %s (ID: %s)", channel_title, channel_id)
                        updated_channels += 1
                        modified_channels_list.append(
                            f"[UPD] {channel_title} ({channel_id}) - {channel_link}"
                        )

            except Exception as e:
                logger.error("Database error for channel %s: %s", channel_id, e)

    # Build detailed report
    if not modified_channels_list:
        logger.info("No channel changes detected.")
        return None

    summary_text = (
        f"Telegram Channel Sync Report\n"
        f"---------------------------\n"
        f"Total channels found: {total_channels}\n"
        f"New channels added: {added_channels}\n"
        f"Channels updated: {updated_channels}\n\n"
        f"Modified Channels List:\n"
        f"{chr(10).join(modified_channels_list)}"
    )

    logger.info("=%s=", "".ljust(48, "="))
    logger.info(summary_text)
    logger.info("=%s=", "".ljust(48, "="))

    return summary_text


async def main():
    while True:
        try:
            summary = await get_subscribed_channels()
            if summary:
                send_simple_email(
                    recipient="s.ansani@kirurusec.com",
                    subject="Telegram Channel Sync Report",
                    body=summary,
                )
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.error("Error in channel list check loop: %s", e)

        logger = logging.getLogger(__name__)
        logger.info("Next check in 12 hours...")
        await asyncio.sleep(12 * 3600)


# Run the process
if __name__ == "__main__":
    setup_logging()
    with client:
        client.loop.run_until_complete(main())
