#!/usr/bin/env python3
"""
Search for email in data leaks and extract all URIs found in password files.
Usage: python search_email_leaks.py <email>
"""

import logging
import sys
from pathlib import Path

from sqlalchemy import text

from utils import (
    clean_string,
    extract_domain,
    extract_password_patterns,
    extract_uris_from_directory,
    get_db_engine,
    parse_soft_passwords,
    setup_logging,
    validate_email,
)

logger = logging.getLogger(__name__)
engine = get_db_engine()


def search_email(email):
    """
    Search for email in the emails_to_send table and extract associated URIs.
    """
    logger.info(f"Searching for email: {email}")

    # Query the database
    search_query = text("""
        SELECT id, recipient_email, analyzed_dir, leak_date, subject
        FROM emails_to_send
        WHERE recipient_email LIKE :email
        LIMIT 100
    """)

    try:
        with engine.begin() as conn:
            result = conn.execute(search_query, {"email": f"{email}"})
            rows = result.fetchall()

        if not rows:
            logger.warning(f"No records found for email: {email}")
            print(f"\n❌ No leak records found for: {email}")
            return

        print(f"\n✅ Found {len(rows)} record(s) for email: {email}\n")
        print("=" * 80)

        for row in rows:
            record_id, recipient_email, analyzed_dir, leak_date, subject = row
            print(f"\nRecord ID: {record_id}")
            print(f"Email: {recipient_email}")
            print(f"Analyzed Directory: {analyzed_dir}")
            print(f"Leak Date: {leak_date}")
            print(f"Subject: {subject}")
            print("-" * 80)

            # Extract URIs from the analyzed_dir
            hosts_with_password = extract_uris_from_directory(analyzed_dir)

            if hosts_with_password:
                print(
                    f"\n📋 Found {len(hosts_with_password)} URI(s) in password files:\n"
                )
                for idx, (domain, password) in enumerate(
                    hosts_with_password.items(), 1
                ):
                    # Mask password for display
                    masked_pwd = (
                        password[:2] + "*" * (len(password) - 4) + password[-2:]
                        if len(password) > 4
                        else "*" * len(password)
                    )
                    print(f"  {idx}. {domain}")
                    print(f"     Password: {masked_pwd}")
            else:
                print("\n⚠️  No URIs found in password files for this leak")

            print("\n" + "=" * 80)

    except Exception as e:
        logger.error(f"Error querying database: {e}")
        print(f"\n❌ Database error: {e}")


def main():
    """Main function."""
    setup_logging()

    # Accept email from CLI arg or interactive prompt
    if len(sys.argv) >= 2:
        email = sys.argv[1].strip()
    else:
        try:
            email = input("Inserisci l'email da cercare: ").strip()
        except EOFError:
            email = ""

    if not email:
        print("❌ Error: Email cannot be empty")
        sys.exit(1)

    # Basic email validation
    if not validate_email(email):
        print(f"⚠️  Warning: '{email}' does not appear to be a valid email address")
        response = input("Continue anyway? (y/n): ").strip().lower()
        if response != "y":
            sys.exit(1)

    search_email(email)


if __name__ == "__main__":
    main()
