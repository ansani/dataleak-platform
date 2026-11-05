#!/usr/bin/env python3
"""
Search for an email in data leaks and output the results as an HTML table,
ready to be included in an HTML report.

The table has only two columns (Field | Value) and one row per detail.
Rows without information are omitted. Path (analyzed directory) and file
names are omitted.

Usage:
  python search_leaked_email_html.py <email>
  python search_leaked_email_html.py <email> --page        # full HTML page
  python search_leaked_email_html.py <email> --title "Report"
  python search_leaked_email_html.py <email> --no-page     # just the <table> fragment (default)
"""

import html
import logging
import sys
from pathlib import Path

from sqlalchemy import text

from cd_leak import _display_value
from utils import (
    clean_string,
    extract_domain,
    extract_password_patterns,
    get_db_engine,
    parse_soft_passwords,
    setup_logging,
    validate_email,
)

setup_logging()

logger = logging.getLogger(__name__)

# (etichetta di colonna fissa: la tabella è a 2 colonne Campo | Valore)

CSS = """
table.cd-leak-table {
    border-collapse: collapse;
    width: 100%;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 Helvetica, Arial, sans-serif;
    font-size: 13px;
    margin: 1em 0;
}
table.cd-leak-table caption {
    caption-side: top;
    text-align: left;
    font-weight: bold;
    font-size: 14px;
    margin-bottom: 0.5em;
}
table.cd-leak-table th,
table.cd-leak-table td {
    border: 1px solid #ccc;
    padding: 4px 8px;
    text-align: left;
    vertical-align: top;
}
table.cd-leak-table thead th {
    background: #f4f4f4;
    position: sticky;
    top: 0;
}
table.cd-leak-table tr:nth-child(even) td {
    background: #fafafa;
}
table.cd-leak-table td.empty {
    color: #999;
    font-style: italic;
}
"""

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
{h1}
{fragment}
</body>
</html>
"""

_engine = None


def get_engine():
    """Return the SQLAlchemy engine (created lazily on first use)."""
    global _engine
    if _engine is None:
        _engine = get_db_engine()
    return _engine


def extract_uris_from_directory(analyzed_dir):
    """
    Extract all URIs from password files in the analyzed directory.
    Returns a dict with domain as key and password as value.
    """
    hosts_with_password = {}
    directory = Path(analyzed_dir)

    if not directory.exists():
        logger.error(f"Directory not found: {analyzed_dir}")
        return hosts_with_password

    logger.info(f"Extracting URIs from directory: {analyzed_dir}")

    try:
        # Find all files in the directory
        for file_path in directory.rglob("*"):
            if not file_path.is_file():
                continue

            file_name = file_path.name.lower()

            # Skip if not a password file
            if "password" not in file_name:
                continue

            logger.debug(f"Processing password file: {file_path}")

            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = clean_string(f.read())

                    # Parse SOFT blocks
                    try:
                        soft_matches = parse_soft_passwords(content)
                        logger.debug(
                            f"Found {len(soft_matches)} SOFT blocks in {file_path}"
                        )
                        for host, user, pwd in soft_matches:
                            domain = extract_domain(host)
                            hosts_with_password[domain] = clean_string(pwd)
                            logger.info(f"Extracted from SOFT block: {domain}")
                    except Exception as e:
                        logger.error(
                            f"Error parsing SOFT blocks in {file_path}: {e}"
                        )

                    # Use centralized pattern extractor for Host:Password and
                    # fallback
                    password_matches = extract_password_patterns(content)
                    for host, pwd in password_matches:
                        domain = extract_domain(host)
                        hosts_with_password[domain] = clean_string(pwd)
                        logger.info(f"Extracted password pattern: {domain}")

            except Exception as e:
                logger.error(f"Error reading file {file_path}: {e}")

    except Exception as e:
        logger.error(f"Error processing directory {analyzed_dir}: {e}")

    logger.info(f"Total URIs extracted: {len(hosts_with_password)}")
    return hosts_with_password


def mask_password(password):
    """Mask a password for safe display (same logic as search_email_leaks)."""
    if not password:
        return ""
    if len(password) > 4:
        return password[:2] + "*" * (len(password) - 4) + password[-2:]
    return "*" * len(password)


def build_details(email):
    """
    Build a list of (field, value) tuples for all leaks found for *email*.
    Path (analyzed directory) and file names are omitted.
    """
    search_query = text("""
        SELECT id, recipient_email, analyzed_dir, leak_date, subject
        FROM emails_to_send
        WHERE recipient_email LIKE :email
        LIMIT 100
    """)

    with get_engine().begin() as conn:
        result = conn.execute(search_query, {"email": email})
        rows = result.fetchall()

    details = []
    if not rows:
        return details

    for i, row in enumerate(rows, 1):
        _record_id, recipient_email, _analyzed_dir, leak_date, subject = row
        prefix = f"Leak {i} — " if len(rows) > 1 else ""

        # Email
        if recipient_email:
            details.append((f"{prefix}Email", recipient_email))

        # Leak date
        if leak_date:
            details.append((f"{prefix}Data leak", str(leak_date)))

        # Subject
        if subject:
            details.append((f"{prefix}Subject", subject))

        # URIs from password files (path and file names omitted)
        hosts_with_password = extract_uris_from_directory(_analyzed_dir)
        for domain, password in hosts_with_password.items():
            if domain:
                details.append((f"{prefix}Dominio", domain))
            if password:
                details.append((f"{prefix}Password", mask_password(password)))

    return details


def escape(value):
    """Escape a string for safe insertion into HTML."""
    return html.escape(str(value), quote=True)


def render_table(details):
    """Render an HTML <table> with two columns (Field | Value).

    Only rows with a non-empty value are included. No caption, no thead.
    """
    parts = []
    parts.append('<table class="cd-leak-table">')
    parts.append("<tbody>")

    if not details:
        parts.append(
            '<tr><td colspan="2" class="empty">Nessuna informazione disponibile</td></tr>'
        )
    else:
        for field, value in details:
            if value is None or value == "":
                continue
            parts.append(
                f"<tr><td>{escape(field)}</td><td>{escape(_display_value(value))}</td></tr>"
            )

    parts.append("</tbody>")
    parts.append("</table>")

    return "\n".join(parts)


def render_page(fragment, title="Leak HTML"):
    """Wrap the fragment into a complete HTML page."""
    h1 = f"<h1>{escape(title)}</h1>" if title else ""
    return PAGE_TEMPLATE.format(title=escape(title), css=CSS, h1=h1, fragment=fragment)


def parse_args(argv):
    options = {"email": None, "page": False, "title": None, "help": False}
    positional = []
    for arg in argv:
        if arg in ("-h", "--help"):
            options["help"] = True
        elif arg == "--page":
            options["page"] = True
        elif arg == "--no-page":
            options["page"] = False
        elif arg == "--title" and len(argv) > argv.index(arg) + 1:
            pass  # gestito sotto
        elif arg.startswith("--title="):
            options["title"] = arg.split("=", 1)[1]
        elif arg.startswith("-"):
            pass
        else:
            positional.append(arg)

    # gestione --title <valore> separato
    i = 0
    while i < len(argv):
        if argv[i] == "--title" and i + 1 < len(argv):
            options["title"] = argv[i + 1]
            i += 2
            continue
        i += 1

    if positional:
        options["email"] = positional[0].strip()
    return options


def main():
    options = parse_args(sys.argv[1:])

    if options["help"]:
        print(__doc__.strip())
        return

    email = options["email"]
    if email is None:
        try:
            email = input("Inserisci l'email da cercare: ").strip()
        except EOFError:
            email = ""

    if not email:
        print("❌ Error: Email cannot be empty", file=sys.stderr)
        sys.exit(1)

    if not validate_email(email):
        print(f"⚠️  Warning: '{email}' does not appear to be a valid email address")

    details = build_details(email)
    fragment = render_table(details)

    if options["page"]:
        output = render_page(fragment, title=options["title"] or f"Leak — {email}")
    else:
        output = fragment

    print(output)


if __name__ == "__main__":
    main()
