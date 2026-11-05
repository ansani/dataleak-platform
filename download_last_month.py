from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from telethon import TelegramClient, errors
from telethon.tl.types import Document, PeerChannel, PeerChat

from config import (
    API_HASH,
    API_ID,
    API_PHONE,
    RABBIT_CONNECTION_STRING,
    UNIQUES_QUEUE,
)
from utils import (
    IGNORED_EXTENSIONS_SET,
    DownloadManager,
    cleanup_stale_downloading,
    download_and_register_document,
    get_db_engine,
    get_normalized_channel_id,
    get_publisher,
    load_active_channels,
    logger,
    setup_logging,
)

# SQL statements for range state
LOAD_RANGE_STATE_SQL = text(
    "SELECT channel_id, min_id, max_id FROM channel_state_monthly"
)
UPSERT_RANGE_STATE_SQL = text("""
    INSERT INTO channel_state_monthly (channel_id, min_id, max_id)
    VALUES (:cid, :min_id, :max_id)
    ON DUPLICATE KEY UPDATE
        min_id = LEAST(channel_state_monthly.min_id, :min_id),
        max_id = GREATEST(channel_state_monthly.max_id, :max_id)
    """)


LOOKBACK_DAYS = 30

environment = get_db_engine()
download_manager = DownloadManager(environment)
client = TelegramClient("last_month_download", API_ID, API_HASH)
channel_names = {}
channel_ranges = {}


def load_range_state():
    ranges = {}
    with environment.begin() as conn:
        result = conn.execute(LOAD_RANGE_STATE_SQL).fetchall()
        for row in result:
            ranges[str(row.channel_id)] = {
                "min_id": int(row.min_id),
                "max_id": int(row.max_id),
            }
    return ranges


def save_range_state(cid: str):
    rng = channel_ranges.get(cid)
    if not rng:
        return

    # 1. Check if the range in DB is already the same to avoid redundant UPDATE (and timestamp change)
    query = text(
        "SELECT min_id, max_id FROM channel_state_monthly WHERE channel_id = :cid"
    )
    with environment.begin() as conn:
        row = conn.execute(query, {"cid": cid}).fetchone()
        if row and row[0] == rng["min_id"] and row[1] == rng["max_id"]:
            return

    with environment.begin() as conn:
        conn.execute(
            UPSERT_RANGE_STATE_SQL,
            {"cid": cid, "min_id": rng["min_id"], "max_id": rng["max_id"]},
        )


def update_range(cid: str, msg_id: int):
    rng = channel_ranges.get(cid)
    if not rng:
        channel_ranges[cid] = {"min_id": msg_id, "max_id": msg_id}
    else:
        rng["min_id"] = min(rng["min_id"], msg_id)
        rng["max_id"] = max(rng["max_id"], msg_id)


def should_skip_by_range(cid: str, msg_id: int) -> bool:
    rng = channel_ranges.get(cid)
    if not rng:
        return False
    return rng["min_id"] <= msg_id <= rng["max_id"]


def normalize_date(dt):
    if dt.tzinfo:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


async def process_message(msg, publisher, channel_id=None):
    cid = str(channel_id) if channel_id is not None else get_normalized_channel_id(msg)
    ch_name = channel_names.get(cid, cid)
    msg_date = normalize_date(msg.date)
    msg_date_str = msg_date.strftime("%Y-%m-%d %H:%M:%S %Z")

    if should_skip_by_range(cid, msg.id):
        return

    update_range(cid, msg.id)

    if not msg.document or not isinstance(msg.document, Document):
        save_range_state(cid)
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
        on_skip_callback=lambda: save_range_state(cid),
        msg_date_str=msg_date_str,
    )

    if success:
        save_range_state(cid)
        return

    # If download_and_register_document returned False, it already logged the reason
    # and triggered on_skip_callback (save_range_state).
    return


async def process_channel(chat, month_start, publisher):

    cid = str(chat)
    nm = cid
    ent = None
    try:
        try:
            ent = await client.get_entity(PeerChannel(int(chat)))
        except Exception:
            try:
                ent = await client.get_entity(PeerChat(int(chat)))
            except Exception:
                try:
                    ent = await client.get_entity(chat)
                except Exception:
                    logger.warning(
                        "Could not resolve entity for channel %s, skipping.", cid
                    )
                    return

        nm = getattr(ent, "title", getattr(ent, "username", cid))
        channel_names[cid] = nm
    except Exception as e:
        logger.warning(
            "Unexpected error resolving entity for channel %s: %s, skipping.", cid, e
        )
        return

    logger.info("Processing last %d days for %s (ID: %s)", LOOKBACK_DAYS, nm, cid)

    try:
        # Use the resolved entity 'ent' instead of 'chat' ID to avoid resolution errors
        async for msg in client.iter_messages(ent):
            msg_date = normalize_date(msg.date)
            if msg_date < month_start:
                break
            if should_skip_by_range(cid, msg.id):
                continue
            await process_message(msg, publisher, cid)
    except Exception as e:
        error_msg = f"Error during monthly catch-up for {nm} (ID: {cid}): {e!s}"
        logger.error(error_msg)
    logger.info("Done %s (ID: %s)", nm, cid)


def main():
    setup_logging()

    # Cleanup stale DOWNLOADING records (older than 24 hours)
    cleanup_stale_downloading(download_manager)

    global channel_ranges
    channel_ranges = load_range_state()
    channels = load_active_channels(environment)

    publisher = get_publisher(RABBIT_CONNECTION_STRING, UNIQUES_QUEUE)

    try:
        client.start(phone=API_PHONE)
    except errors.ApiIdInvalidError:
        logger.error("Invalid API credentials.")
        return

    month_start = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    async def runner():
        for chat in channels:
            await process_channel(chat, month_start, publisher)
        await client.disconnect()

    client.loop.run_until_complete(runner())


if __name__ == "__main__":
    main()
