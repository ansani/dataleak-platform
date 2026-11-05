import asyncio
import json
import random
import re
import threading
from urllib.parse import urlparse

from telethon import TelegramClient, errors
from telethon.tl.functions.channels import GetParticipantRequest, JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

import config
from utils import LinkManager, RabbitMQConsumer, get_db_engine, logger, setup_logging


# Patterns for invalid/suspicious URIs
BOT_USERNAME_PATTERN = re.compile(r"(?i)bot$|_bot$|^bot_")
INVALID_USERNAME_PATTERN = re.compile(r"(?i)[^a-z0-9_]")
STAR_PAYMENT_INVITE_HINTS = [
    "stars",
    "premium",
    "vip",
    "paid",
    "subscription",
]


def validate_subscription_uri(uri: str) -> tuple[bool, str]:
    """
    Validate URI before attempting subscription.
    Returns (is_valid, reason_if_invalid).
    """
    if not uri or not uri.startswith("https://t.me/"):
        return False, "Not a t.me link"

    parsed = urlparse(uri)
    clean_path = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    path_parts = [p for p in parsed.path.split("/") if p]

    # Check for query parameters indicating bot/start links
    if parsed.query:
        if parsed.query.startswith("start=") or "start=" in parsed.query:
            return False, "Bot start link (contains ?start= parameter)"

    # Check for invite links (+hash or joinchat)
    if "t.me/+" in clean_path or "t.me/joinchat/" in clean_path:
        # Invite links - check for suspicious hints in the hash
        invite_hash = ""
        if "t.me/+" in clean_path:
            invite_hash = clean_path.split("/")[-1].replace("+", "")
        elif "joinchat/" in clean_path:
            invite_hash = clean_path.split("joinchat/")[-1].split("/")[0]

        # Check for hints that this might be a paid/STARS invite
        if any(hint in invite_hash.lower() for hint in STAR_PAYMENT_INVITE_HINTS):
            return False, f"Suspicious invite link (possible STARS payment required): {invite_hash}"

        # Invite links are generally valid to try
        return True, ""

    # Public channel links: t.me/username
    if len(path_parts) == 1:
        username = path_parts[0].lower()

        # Filter bot usernames
        if BOT_USERNAME_PATTERN.search(username):
            return False, f"Bot username detected: {username}"

        # Filter invalid username format
        if INVALID_USERNAME_PATTERN.search(username):
            return False, f"Invalid username format: {username}"

        # Filter very short usernames (likely not real channels)
        if len(username) < 3:
            return False, f"Username too short: {username}"

        # Filter suspicious keywords
        suspicious_keywords = ["bot", "spam", "fake", "test", "temp"]
        if any(kw in username for kw in suspicious_keywords):
            return False, f"Suspicious username keyword: {username}"

        return True, ""

    # Message links or other multi-segment paths
    if len(path_parts) > 1:
        return False, f"Message link or invalid path: {uri}"

    return False, f"Unrecognized URI format: {uri}"

# RabbitMQ Configuration
QUEUE_NAME = config.URI_TELEGRAM_QUEUE


class PermanentError(Exception):
    """Exception raised for errors that cannot be resolved by retrying."""


class ChannelLimitReachedError(Exception):
    """Exception raised when account has reached the channel limit.
    This signals the script should exit immediately to avoid losing links.
    """
    def __init__(self, message: str, uri: str = ""):
        super().__init__(message)
        self.uri = uri


async def join_telegram_channel(client, uri: str) -> str:
    """
    Attempt to join a Telegram channel/group via URI.
    Returns 'NEW_SUBSCRIBED', 'ALREADY_SUBSCRIBED', 'JOIN_REQUESTED', or raises an Exception.
    """
    # Strip query parameters from URI (e.g., ?start=...)
    parsed = urlparse(uri)
    clean_uri = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    # Filter out message links (e.g., t.me/channel/123)
    # A channel link is typically t.me/username or t.me/+hash.
    # If the path contains more than one segment after the username (e.g.,
    # /username/123), it's a message.
    path_segments = [s for s in parsed.path.split("/") if s]
    if len(path_segments) > 1 and not any(x in clean_uri for x in ("joinchat/", "/+")):
        raise PermanentError(f"URI is a message link, not a channel join link: {uri}")

    try:
        # 1. Attempt to join
        if "t.me/+" in clean_uri or "t.me/joinchat/" in clean_uri:
            if "joinchat/" in clean_uri:
                invite_hash = clean_uri.split("joinchat/")[-1].split("/")[0]
            else:
                invite_hash = clean_uri.split("/")[-1].replace("+", "")

            try:
                await client(ImportChatInviteRequest(invite_hash))
            except errors.UserAlreadyParticipantError:
                return "ALREADY_SUBSCRIBED"
            except errors.FloodWaitError:
                raise
            except Exception as e:
                err_msg = str(e).lower()
                if "expired" in err_msg or "not valid" in err_msg:
                    raise PermanentError(f"Invite link expired or invalid: {e}")
                if "successfully requested to join" in err_msg:
                    return "JOIN_REQUESTED"
                if "too many channels" in err_msg:
                    raise ChannelLimitReachedError(
                        f"Account limit reached: too many channels: {e}", uri
                    )
                if "stars_payment_required" in err_msg or "payment required" in err_msg:
                    raise PermanentError(f"Invite requires STARS payment: {e}")
                logger.error("Invite request failed for %s: %s", uri, e)
                raise e
        else:
            try:
                entity = await client.get_entity(clean_uri)
            except errors.FloodWaitError:
                raise
            except Exception as e:
                err_msg = str(e).lower()
                if "inputpeeruser" in err_msg or "cannot cast" in err_msg:
                    raise PermanentError(
                        f"URI points to a user/bot, not a channel: {e}"
                    )
                raise PermanentError(f"Cannot resolve entity for {uri}: {e}")

            # Check membership before attempting join
            try:
                await client(GetParticipantRequest(entity, "me"))
                return "ALREADY_SUBSCRIBED"
            except errors.UserNotParticipantError:
                pass
            except errors.FloodWaitError:
                raise
            except Exception:
                pass

            try:
                await client(JoinChannelRequest(entity))
            except errors.UserAlreadyParticipantError:
                return "ALREADY_SUBSCRIBED"
            except errors.FloodWaitError:
                raise
            except Exception as e:
                err_msg = str(e).lower()
                if "successfully requested to join" in err_msg:
                    return "JOIN_REQUESTED"
                if "too many channels" in err_msg:
                    raise ChannelLimitReachedError(
                        f"Account limit reached: too many channels: {e}", uri
                    )
                if "cannot cast" in err_msg:
                    raise PermanentError(
                        f"URI points to a user/bot, not a channel: {e}"
                    )
                logger.error("Join request failed for %s: %s", uri, e)
                raise e

        # 2. Verify membership status
        try:
            entity = await client.get_entity(clean_uri)
            await client(GetParticipantRequest(entity, "me"))
            return "NEW_SUBSCRIBED"
        except errors.UserNotParticipantError:
            return "JOIN_REQUESTED"
        except errors.FloodWaitError:
            raise
        except Exception as e:
            logger.warning("Could not verify membership for %s: %s", uri, e)
            return "NEW_SUBSCRIBED"

    except Exception as e:
        if isinstance(e, (errors.FloodWaitError, PermanentError)):
            raise e
        logger.error("Unexpected error joining channel %s: %s", uri, e)
        raise e


async def process_subscription(client, uri, ch, method, link_manager):
    """Async logic to join channel and update DB."""
    try:
        # Quick membership check for public channels before anti-flood
        parsed = urlparse(uri)
        clean_path = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        is_invite = "t.me/+" in clean_path or "t.me/joinchat/" in clean_path

        if not is_invite:
            try:
                entity = await client.get_entity(clean_path)
                await client(GetParticipantRequest(entity, "me"))
                logger.info("Already subscribed to %s, skipping", uri)
                link_manager.update_status(uri, "ALREADY_SUBSCRIBED")
                ch.connection.add_callback_threadsafe(
                    lambda: ch.basic_ack(delivery_tag=method.delivery_tag)
                )
                return
            except errors.UserNotParticipantError:
                pass
            except errors.FloodWaitError:
                raise
            except Exception:
                pass

        # Anti-flood delay: wait between 5 and 10 minutes
        wait_time = random.randint(300, 600)
        logger.info(
            "Anti-flood delay: waiting %d seconds before processing %s", wait_time, uri
        )
        await asyncio.sleep(wait_time)

        status = await join_telegram_channel(client, uri)
        link_manager.update_status(uri, status)

        # RabbitMQ Ack
        ch.connection.add_callback_threadsafe(
            lambda: ch.basic_ack(delivery_tag=method.delivery_tag)
        )
        logger.info("Successfully processed subscription for %s -> %s", uri, status)
    except errors.FloodWaitError as e:
        wait_time = min(e.seconds, 3600)
        logger.warning(
            "FloodWait detected for %s: wait %d seconds. Pausing worker for %d seconds...",
            uri,
            e.seconds,
            wait_time,
        )
        await asyncio.sleep(wait_time)
        # Transient error: requeue the message
        ch.connection.add_callback_threadsafe(
            lambda: ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        )
    except PermanentError as e:
        logger.error("Permanent error processing subscription for %s: %s", uri, e)
        link_manager.update_status(uri, "FAILED", str(e))
        ch.connection.add_callback_threadsafe(
            lambda: ch.basic_ack(delivery_tag=method.delivery_tag)
        )
    except ChannelLimitReachedError as e:
        logger.critical("CHANNEL LIMIT REACHED for %s: %s. Exiting to preserve remaining links.", uri, e)
        link_manager.update_status(uri, "CHANNEL_LIMIT", str(e))
        # Signal the main loop to exit by stopping the consumer
        ch.connection.add_callback_threadsafe(
            lambda: ch.basic_ack(delivery_tag=method.delivery_tag)
        )
        # Re-raise to be caught by the main loop
        raise e
    except Exception as e:
        logger.error("Unexpected error processing subscription for %s: %s", uri, e)
        link_manager.update_status(uri, "FAILED", str(e))
        ch.connection.add_callback_threadsafe(
            lambda: ch.basic_ack(delivery_tag=method.delivery_tag)
        )


def main_callback(ch, method, properties, body, client, link_manager, loop):
    """Sync callback for RabbitMQ consumer."""
    try:
        data = json.loads(body.decode("utf-8").strip())
        uri = (
            data.get("uri") if isinstance(data, dict) else body.decode("utf-8").strip()
        )
    except json.JSONDecodeError:
        uri = body.decode("utf-8").strip()

    if not uri:
        logger.warning("Message body did not contain a valid URI: %s", body)
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    # Pre-validate URI before scheduling
    is_valid, reason = validate_subscription_uri(uri)
    if not is_valid:
        logger.warning("Rejected URI (pre-validation): %s - %s", uri, reason)
        link_manager.update_status(uri, "FAILED")
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    logger.info("Received URI for subscription: %s", uri)

    # Schedule the async join logic on the main loop
    asyncio.run_coroutine_threadsafe(
        process_subscription(client, uri, ch, method, link_manager), loop
    )


async def main():
    setup_logging()

    # Initialize LinkManager
    link_manager = LinkManager(get_db_engine())

    # Event to signal channel limit reached
    channel_limit_reached = asyncio.Event()

    # Use a retry loop for the Telegram client connection
    while True:
        try:
            client = TelegramClient(
                "SUBSCRIPTION_CHANNEL", config.API_ID, config.API_HASH
            )
            await client.start(phone=config.API_PHONE)
            logger.info("Telegram client started.")

            loop = asyncio.get_running_loop()

            # Wrap callback to catch ChannelLimitReachedError
            def wrapped_callback(ch, method, properties, body):
                try:
                    main_callback(ch, method, properties, body, client, link_manager, loop)
                except ChannelLimitReachedError:
                    logger.critical("Channel limit reached, signalling main loop to exit")
                    channel_limit_reached.set()

            # Use the robust RabbitMQConsumer from utils
            consumer = RabbitMQConsumer(
                url=config.RABBIT_CONNECTION_STRING,
                queue=QUEUE_NAME,
                callback=wrapped_callback,
                prefetch_count=1,
            )

            # Start consumer in a thread since RabbitMQConsumer.start() is
            # blocking
            consumer_thread = threading.Thread(target=consumer.start, daemon=True)
            consumer_thread.start()

            # Keep the main loop alive, checking for channel limit
            while not channel_limit_reached.is_set():
                await asyncio.sleep(60)

            if channel_limit_reached.is_set():
                logger.critical("Channel limit reached. Stopping subscription worker to preserve remaining links.")
                consumer.stop()
                break

        except (ConnectionError, errors.ConnectionError) as e:
            logger.error(
                "Connection to Telegram failed: %s. Retrying in 60 seconds...", e
            )
            await asyncio.sleep(60)
        except Exception as e:
            logger.error("Unexpected error in subscription main loop: %s", e)
            await asyncio.sleep(30)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
