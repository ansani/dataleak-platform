import argparse
import hashlib
import hmac
import logging
import random
import smtplib
import time
import urllib.parse
from contextlib import contextmanager
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import config
from utils import AnalysisManager, get_db_engine, html_to_text, setup_logging, validate_email

setup_logging()  # Configure logging using the function from utils.py
logger = logging.getLogger(__name__)

# Get database engine
engine = get_db_engine()

# Postal Cloud SMTP Configuration from config.py
SMTP_HOST = config.SMTP_HOST
SMTP_PORT = config.SMTP_PORT
SMTP_USER = config.SMTP_USER
SMTP_PASSWORD = config.SMTP_PASSWORD
SENDER_EMAIL = config.SENDER_EMAIL
SENDER_NAME = config.SENDER_NAME
UNSUBSCRIBE_URL = config.UNSUBSCRIBE_URL
UNSUBSCRIBE_SECRET = config.UNSUBSCRIBE_SECRET

if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SENDER_EMAIL]):
    logger.error("Missing Postal SMTP parameters in config.py")
    raise ValueError(
        "Missing Postal SMTP parameters (host, user, password or sender email) in config.py"
    )

# Function to generate an HMAC token


def generate_unsubscribe_token(email, secret=UNSUBSCRIBE_SECRET):
    h = hmac.new(secret.encode("utf-8"), email.encode("utf-8"), hashlib.sha256)
    return h.hexdigest()[:16]




def build_mime_message(recipient_email, subject, html_content, unsubscribe_url=None):
    """Build the multipart/alternative MIME message with text/plain + text/html.

    Explicitly sets the required Date and Message-Id headers that Postal
    would otherwise leave out, and adds the dynamic List-Unsubscribe headers.
    """
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
    msg["To"] = recipient_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-Id"] = make_msgid(domain="kirurusec.com")
    if unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    text_part = MIMEText(html_to_text(html_content), "plain", "utf-8")
    html_part = MIMEText(html_content, "html", "utf-8")
    msg.attach(text_part)
    msg.attach(html_part)
    return msg


# Update status to "MAIL_SENT" in the analyzed_leaks table


def update_status_to_mail_sent(analysis_manager, analyzed_dir):
    analysis_manager.update_leak_status(analyzed_dir, "MAIL_SENT")
    logger.info("Updated status to MAIL_SENT for: %s", analyzed_dir)


def _mark_dirs_as_mail_sent(analysis_manager, analyzed_dirs):
    """Mark the given analyzed directories as MAIL_SENT (deduplicated)."""
    for analyzed_dir in sorted(analyzed_dirs):
        update_status_to_mail_sent(analysis_manager, analyzed_dir)


# Update status to "SENT" in the emails_to_send table


def update_email_status_to_sent(analysis_manager, email_id):
    analysis_manager.mark_email_as_sent(email_id)
    logger.info("Email status updated to SENT and content cleared for id: %s", email_id)


def _connect_smtp():
    server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
    server.login(SMTP_USER, SMTP_PASSWORD)
    return server


def _close_smtp(server):
    try:
        server.quit()
    except (smtplib.SMTPException, OSError):
        try:
            server.close()
        except (smtplib.SMTPException, OSError):
            logger.debug("SMTP connection already closed", exc_info=True)


@contextmanager
def _smtp_connection():
    """Context manager yielding a connected SMTP server (single-use)."""
    server = _connect_smtp()
    try:
        yield server
    finally:
        _close_smtp(server)


# Function to send an email via Postal SMTP with dynamic List-Unsubscribe
# header, token and One-Click support
def send_stored_email(
    smtp_server,
    email_id,
    analyzed_dir,
    recipient_email,
    subject,
    html_content,
    analysis_manager,
    sent_recipients=None,
    test_mode=False,
    test_email=None,
):
    target_email = test_email if test_mode else recipient_email
    if (
        not test_mode
        and sent_recipients is not None
        and recipient_email in sent_recipients
    ):
        logger.info("Email already sent to %s, skipping send", recipient_email)
        update_email_status_to_sent(analysis_manager, email_id)
        return None

    # Build the dynamic unsubscribe URL with token
    encoded_email = urllib.parse.quote(target_email)
    token = generate_unsubscribe_token(target_email)
    unsubscribe_url_dynamic = (
        f"{UNSUBSCRIBE_URL}?email={encoded_email}&token={token}"
    )

    msg = build_mime_message(
        target_email, subject, html_content, unsubscribe_url=unsubscribe_url_dynamic
    )

    try:
        if smtp_server is None:
            with _smtp_connection() as server:
                server.send_message(msg)
        else:
            smtp_server.send_message(msg)
    except smtplib.SMTPServerDisconnected:
        raise
    except smtplib.SMTPException as e:
        logger.error("Error sending email to %s: %s", target_email, str(e))
        # Update the emails_to_send row to FAILED and record last_attempt
        if not test_mode:
            try:
                analysis_manager.update_email_status(email_id, "FAILED")
                logger.info("Email status updated to FAILED for id: %s", email_id)
            except SQLAlchemyError as ee:
                logger.error(
                    "Error updating email status to FAILED for id %s: %s", email_id, ee
                )
        return None

    logger.info(
        "Email sent to %s with List-Unsubscribe header: %s",
        target_email,
        unsubscribe_url_dynamic,
    )
    if not test_mode:
        analysis_manager.record_email_sent(
            email_id, recipient_email, subject, analyzed_dir
        )
        logger.info(
            "Recorded sent email for %s (id=%s)", recipient_email, email_id
        )
        return analyzed_dir
    else:
        logger.info(
            "Test mode: email sent to %s without updating the database",
            target_email,
        )
        return None


# Function to count and show details of emails to send with optional
# filter and ordering
def preview_emails_to_send(analysis_manager, limit, email_filter, sort_by_leak_date):
    emails_data = analysis_manager.get_pending_emails(
        limit, email_filter, sort_by_leak_date
    )

    if not emails_data:
        logger.info(
            "No pending emails found in the database with the specified filter."
        )
        return None

    email_count = len(emails_data)
    logger.info(
        "Number of emails to send: %d (filter: %s, order: %s)",
        email_count,
        email_filter,
        ("leak_date DESC" if sort_by_leak_date else "created_at DESC"),
    )
    logger.info("Recipients:")
    for email_data in emails_data:
        recipient_email = email_data[2]
        leak_date = email_data[5]
        leak_date_str = (
            f" - Leak Date: {leak_date}"
            if leak_date
            else " - Leak Date: Not specified"
        )
        logger.info("  - %s%s", recipient_email, leak_date_str)

    return emails_data


# Function to send a test email
def send_test_email(analysis_manager, test_email):
    with engine.begin() as conn:
        min_id = conn.execute(
            text("SELECT MIN(id) FROM emails_to_send WHERE status = 'PENDING'")
        ).scalar()
        max_id = conn.execute(
            text("SELECT MAX(id) FROM emails_to_send WHERE status = 'PENDING'")
        ).scalar()

    if min_id is None or max_id is None:
        logger.info("No pending emails available for test.")
        return

    pick = random.randint(min_id, max_id)
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT id, analyzed_dir, recipient_email, subject, html_content, leak_date
                FROM emails_to_send
                WHERE id >= :id AND status = 'PENDING'
                ORDER BY id
                LIMIT 1
            """),
            {"id": pick},
        ).fetchone()

    if not row:
        logger.info("No pending emails available for test.")
        return

    email_id, analyzed_dir, _, subject, html_content, _ = row

    logger.info("Sending test email to: %s using email id: %s", test_email, email_id)
    send_stored_email(
        None,
        email_id,
        analyzed_dir,
        None,
        subject,
        html_content,
        analysis_manager,
        test_mode=True,
        test_email=test_email,
    )


# Main function to process and send stored emails
def process_stored_emails(
    analysis_manager, limit, email_filter, sort_by_leak_date, no_ask=False
):
    emails_data = preview_emails_to_send(
        analysis_manager, limit, email_filter, sort_by_leak_date
    )
    if not emails_data:
        return

    # Ask user for confirmation only if no_ask is not True
    if not no_ask:
        while True:
            confirmation = (
                input("\nProceed with sending emails? (y/n): ").strip().lower()
            )
            if confirmation in ["y", "n"]:
                break

        if confirmation == "n":
            logger.info("Email sending cancelled by user.")
            return

    processed_count = len(emails_data)
    logger.info("Processing %d emails in PENDING state", processed_count)

    sent_recipients = analysis_manager.get_sent_recipients(
        [email_data[2] for email_data in emails_data]
    )
    sent_dirs = set()
    server = _connect_smtp()
    try:
        for email_data in emails_data:
            email_id, analyzed_dir, recipient_email, subject, html_content, _ = email_data
            if not validate_email(recipient_email):
                logger.warning(
                    "Skipping invalid email address: %s (id=%s)", recipient_email, email_id
                )
                try:
                    analysis_manager.update_email_status(email_id, "FAILED")
                except SQLAlchemyError:
                    logger.exception("Failed to mark invalid email FAILED for id %s", email_id)
                continue
            logger.info(
                "Sending email for: %s, directory: %s", recipient_email, analyzed_dir
            )
            attempts = 2
            while attempts:
                try:
                    if send_stored_email(
                        server,
                        email_id,
                        analyzed_dir,
                        recipient_email,
                        subject,
                        html_content,
                        analysis_manager,
                        sent_recipients,
                    ):
                        sent_dirs.add(analyzed_dir)
                    break
                except smtplib.SMTPServerDisconnected:
                    attempts -= 1
                    if attempts:
                        logger.warning(
                            "SMTP connection lost; reconnecting and retrying %s",
                            recipient_email,
                        )
                        _close_smtp(server)
                        server = _connect_smtp()
                    else:
                        logger.error(
                            "SMTP disconnected twice for %s; marking as FAILED and skipping",
                            recipient_email,
                        )
                        try:
                            analysis_manager.update_email_status(email_id, "FAILED")
                        except SQLAlchemyError:
                            logger.exception("Failed to update email status to FAILED for id %s", email_id)
                        break
    except KeyboardInterrupt:
        logger.warning(
            "Interrupted by user; marking %d already-processed directories as MAIL_SENT before exit",
            len(sent_dirs),
        )
        _mark_dirs_as_mail_sent(analysis_manager, sent_dirs)
        raise
    finally:
        _close_smtp(server)

    _mark_dirs_as_mail_sent(analysis_manager, sent_dirs)


# Funzione principale
def main():
    parser = argparse.ArgumentParser(
        description="Script to send stored emails from MySQL via Postal SMTP."
    )
    parser.add_argument(
        "limit",
        type=int,
        nargs="?",
        default=0,
        help="Number of emails to process (0 for all)",
    )
    parser.add_argument(
        "-f",
        "--filter",
        type=str,
        default=None,
        help="String to filter recipient emails (e.g. '%%@example.com')",
    )
    parser.add_argument(
        "-s",
        "--sort-by-leak-date",
        action="store_true",
        help="Sort emails by leak date descending",
    )
    parser.add_argument(
        "-t",
        "--test",
        type=str,
        default=None,
        help="Send a test email to a specified address (e.g. test@example.com)",
    )
    parser.add_argument(
        "-n",
        "--noask",
        action="store_true",
        help="Do not ask for confirmation before sending emails",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run in loop mode: send 150 emails every 10 minutes",
    )
    args = parser.parse_args()

    if args.limit < 0:
        logger.error("The 'limit' parameter must be a non-negative integer")
        return

    try:
        engine = get_db_engine()
        analysis_manager = AnalysisManager(engine)

        if args.test:
            logger.info("Test mode enabled: sending a test email to %s", args.test)
            send_test_email(analysis_manager, args.test)
        elif args.loop:
            logger.info("Starting send_mail in loop mode: 150 emails every 10 minutes")
            while True:
                try:
                    logger.info("Loop iteration: processing up to 150 emails")
                    process_stored_emails(analysis_manager, 150, None, False, no_ask=True)
                except Exception:  # keep daemon alive across transient SMTP/DB errors
                    logger.exception(
                        "Error in loop iteration (SMTP/DB issue?), continuing"
                    )
                logger.info("Loop iteration finished. Sleeping for 10 minutes...")
                time.sleep(600)
        else:
            filter_info = f" with filter '{args.filter}'" if args.filter else ""
            sort_info = " sorted by leak_date DESC" if args.sort_by_leak_date else ""
            noask_info = " without confirmation" if args.noask else ""
            logger.info(
                "Starting send_stored_emails with limit=%d%s%s%s",
                args.limit,
                filter_info,
                sort_info,
                noask_info,
            )
            process_stored_emails(
                analysis_manager,
                args.limit,
                args.filter,
                args.sort_by_leak_date,
                args.noask,
            )
            logger.info("Finished send_stored_emails process")

    except Exception as e:  # noqa: BLE001 - top-level guard: log and exit gracefully
        logger.error("Error in the email sending process: %s", str(e))


if __name__ == "__main__":
    main()
