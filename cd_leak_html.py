#!/usr/bin/env python3
"""
Esporta le informazioni di leak per un'email come tabella HTML,
pronta per essere inclusa in un report HTML.

La tabella ha solo due colonne (Campo | Valore) e una riga per ogni
dettaglio presente. Le righe senza informazioni non vengono inserite.
Vengono omessi i dati sul path (directory della leak) e sui nomi dei file.

Uso:
  python cd_leak_html.py user@example.com
  python cd_leak_html.py user@example.com --page        # pagina HTML completa
  python cd_leak_html.py user@example.com --title "Leak trovati"
  python cd_leak_html.py user@example.com --no-page     # solo frammento <table> (default)
"""

import html
import sys

from cd_leak import _display_value, find_leak_dirs, guess_pc_name, load_pc_info
from utils import setup_logging, validate_email

setup_logging()

# (etichetta di colonna, chiave nel dizionario dei campi PC)
COLUMNS = [
    ("Data", "Date"),
    ("HWID", "HWID"),
    ("PC Name", "PC Name"),
    ("User Name", "User Name"),
    ("Windows", "Windows"),
    ("IP", "IP"),
    ("Paese", "Country"),
    ("Città", "City"),
    ("Lingua", "Language"),
    ("Fuso orario", "Time Zone"),
    ("Processore", "Processor"),
    ("RAM", "RAM"),
    ("VideoCard", "VideoCard"),
    ("Antivirus", "Antivirus"),
]

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


def escape(value):
    """Escape una stringa per l'inserimento sicuro in HTML."""
    return html.escape(str(value), quote=True)


def build_row(email, directory):
    """Costruisce un dizionario campo->valore per una singola directory di leak."""
    fields, _extras, _sources = load_pc_info(directory)

    # PC Name non viene mai preso dal path: viene letto dai file info oppure
    # indovinato dal nome della directory (solo l'hostname, mai il percorso).
    if "PC Name" not in fields:
        guessed = guess_pc_name(directory)
        if guessed:
            fields["PC Name"] = guessed

    row = {"email": email}
    row.update(fields)
    return row


def render_table(email, rows):
    """Rende un frammento HTML <table> a 2 colonne (Campo | Valore).

    Viene inserita una riga solo per i campi che hanno un valore non vuoto.
    """
    parts = []
    parts.append('<table class="cd-leak-table">')
    parts.append("<tbody>")

    detail_rows = []
    for i, row in enumerate(rows, 1):
        leak_prefix = f"Leak {i} — " if len(rows) > 1 else ""
        for label, key in COLUMNS:
            value = row.get(key, "")
            if value is None or value == "":
                continue
            detail_rows.append(
                (f"{leak_prefix}{escape(label)}", escape(_display_value(value)))
            )

    if not detail_rows:
        parts.append(
            '<tr><td colspan="2" class="empty">Nessuna informazione disponibile</td></tr>'
        )
    else:
        for label, value in detail_rows:
            parts.append(f"<tr><td>{label}</td><td>{value}</td></tr>")

    parts.append("</tbody>")
    parts.append("</table>")

    return "\n".join(parts)


def render_page(fragment, title="Leak HTML"):
    """Avvolge il frammento in una pagina HTML completa."""
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
            email = input("Email to search: ").strip()
        except EOFError:
            email = ""

    if not email:
        print("Error: email cannot be empty", file=sys.stderr)
        sys.exit(1)

    if not validate_email(email):
        print(f"Warning: '{email}' does not look like a valid email", file=sys.stderr)

    rows = find_leak_dirs(email)

    # Costruisce i dizionari fila (omettendo path e nomi file)
    data_rows = [build_row(email, directory) for directory, _leak_date in rows]

    fragment = render_table(email, data_rows)

    if options["page"]:
        output = render_page(fragment, title=options["title"] or f"Leak — {email}")
    else:
        output = fragment

    print(output)


if __name__ == "__main__":
    main()
