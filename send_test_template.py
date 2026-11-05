#!/usr/bin/env python3
"""Send a test email built from the real template with random but coherent data.

This script is meant for verifying template fixes and anti-spam configuration
in realtime. It does NOT touch the database: no emails are stored in
emails_to_send, no statuses are updated and nothing is recorded in sent_emails.

Emails only list the affected websites: passwords are never included.

Usage:
    python send_test_template.py <recipient_email> [--count N]
"""

import argparse
import random
import sys

from analyze import load_email_template
from send_mail import (
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    UNSUBSCRIBE_URL,
    build_mime_message,
    generate_unsubscribe_token,
)
from utils import setup_logging

setup_logging()

BIG_HOSTS = [
    "https://facebook.com",
    "https://www.google.com",
    "https://www.amazon.com",
    "https://mail.yahoo.com",
    "https://outlook.live.com",
    "https://www.instagram.com",
    "https://twitter.com",
    "https://www.linkedin.com",
    "https://www.reddit.com",
    "https://netflix.com",
    "https://www.paypal.com",
    "https://www.ebay.com",
    "https://accounts.google.com",
    "https://web.whatsapp.com",
    "https://steamcommunity.com",
    "https://discord.com",
    "https://www.dropbox.com",
    "https://github.com",
    "https://www.binance.com",
    "https://www.coinbase.com",
    "https://www.adobe.com",
    "https://www.spotify.com",
    "https://www.airbnb.com",
    "https://www.booking.com",
    "https://www.tiktok.com",
    "https://www.snapchat.com",
    "https://www.twitch.tv",
    "https://www.telegram.org",
    "https://www.microsoft.com",
    "https://appleid.apple.com",
]


def generate_hosts_with_password(count):
    hosts = random.sample(BIG_HOSTS, min(count, len(BIG_HOSTS)))
    return {host: "" for host in hosts}


def send_test_email(recipient_email, count):
    import smtplib
    import urllib.parse

    hosts_with_password = generate_hosts_with_password(count)
    total_password_count = random.randint(
        max(count + 20, 60), max(count + 20, 60) * 4
    )

    subject, html_content = load_email_template(
        recipient_email, hosts_with_password, total_password_count
    )

    token = generate_unsubscribe_token(recipient_email)
    encoded_email = urllib.parse.quote(recipient_email)
    unsubscribe_url = "%s?email=%s&token=%s" % (
        UNSUBSCRIBE_URL,
        encoded_email,
        token,
    )

    msg = build_mime_message(
        recipient_email, subject, html_content, unsubscribe_url=unsubscribe_url
    )

    print("Subject: %s" % subject)
    print("To: %s" % recipient_email)
    print("List-Unsubscribe: %s" % unsubscribe_url)
    print("Services shown: %d, total in leak: %d" % (
        len(hosts_with_password),
        total_password_count,
    ))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)

    print("Test email sent to %s (no database write performed)" % recipient_email)


def main():
    parser = argparse.ArgumentParser(
        description="Send a test email using the real template with random coherent data (no DB writes)."
    )
    parser.add_argument("recipient_email", type=str, help="Recipient email address")
    parser.add_argument(
        "--count",
        type=int,
        default=10,
        help="Number of host entries to display (max 10 in template)",
    )
    args = parser.parse_args()

    if args.count < 1:
        print("--count must be >= 1", file=sys.stderr)
        sys.exit(1)

    send_test_email(args.recipient_email, args.count)


if __name__ == "__main__":
    main()
