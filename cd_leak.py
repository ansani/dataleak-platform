#!/usr/bin/env python3
"""
Find the leak directory for an email and output its path.
Designed to be used with:  cd $(python cd_leak.py <email>)

Once the directory is located, the PC information files found inside it
(System.txt, UserInformation.txt, information.txt, pc_info.json, ...) are
parsed and printed on stderr, so they never interfere with the path written
to stdout.

If multiple directories are found, shows an interactive picker.

Usage:
  cd $(python cd_leak.py user@example.com)
  python cd_leak.py user@example.com              # shows list + PC info
  python cd_leak.py user@example.com --no-info    # path only
  python cd_leak.py user@example.com -i           # PC info only (no path)
"""

import json
import os
import re
import sys
from pathlib import Path

from sqlalchemy import text

from utils import get_db_engine, setup_logging, validate_email

# File names (lowercase) that usually carry the machine information.
INFO_FILE_NAMES = {
    "system.txt",
    "systeminfo.txt",
    "system_info.txt",
    "system-info.txt",
    "system summary.txt",
    "system_summary.txt",
    "userinformation.txt",
    "user_information.txt",
    "user-information.txt",
    "information.txt",
    "information.json",
    "info.txt",
    "info.json",
    "pc.txt",
    "pc.json",
    "pc_info.txt",
    "pc_info.json",
    "pc-information.txt",
    "pc_information.txt",
    "pcinfo.txt",
    "pcinfo.json",
    "pc information.txt",
    "computer.txt",
    "computer.json",
    "computerinfo.txt",
    "computer_info.txt",
    "computer_info.json",
    "computer name.txt",
    "machine.txt",
    "machine_info.txt",
    "machine_info.json",
    "host.txt",
    "hostname.txt",
    "user_info.txt",
    "user_info.json",
    "hardware.txt",
    "hardware.json",
    "system.json",
}

# Suffixes that mark a file as a plausible machine-info file.
INFO_FILE_SUFFIXES = ("_info.json", "_info.txt", "_information.txt", "_information.json")

# How deep (relative to the leak directory) to look for the info file.
MAX_INFO_DEPTH = 3

# Max size of a file we are willing to read/parse.
MAX_INFO_FILE_SIZE = 2 * 1024 * 1024

# Max length of a single displayed value. Stealers happily dump whole process
# and software lists (thousands of characters) which would flood the output.
MAX_DISPLAY_VALUE = 160

# Order in which the parsed fields are displayed.
IDENTITY_FIELDS = ["Date", "HWID"]
SYSTEM_FIELDS = [
    "Windows",
    "User Name",
    "PC Name",
    "Display Resolution",
    "Antivirus",
    "IP",
    "Country",
    "City",
    "Language",
    "Time Zone",
    "Install Date",
]
HARDWARE_FIELDS = [
    "Processor",
    "Cores",
    "Threads",
    "RAM",
    "VideoCard",
    "Motherboard",
    "Disk",
]

# Label variants seen across the different stealer families (RedLine, Raccoon,
# Vidar, Lumma, StealC, Meta, ...) mapped to the canonical display label.
LABEL_ALIASES = {
    # Identity
    "date": "Date",
    "date time": "Date",
    "datetime": "Date",
    "local date": "Date",
    "local time": "Date",
    "log date": "Date",
    "log time": "Date",
    "leak date": "Date",
    "scan date": "Date",
    "timestamp": "Date",
    "time": "Date",
    "hwid": "HWID",
    "hw id": "HWID",
    "hardware id": "HWID",
    "hardwareid": "HWID",
    "machine id": "HWID",
    "machine guid": "HWID",
    "guid": "HWID",
    "uid": "HWID",
    "uuid": "HWID",
    "device id": "HWID",
    # Operating system
    "windows": "Windows",
    "windows version": "Windows",
    "os": "Windows",
    "os name": "Windows",
    "os version": "Windows",
    "operating system": "Windows",
    "operation system": "Windows",
    "platform": "Windows",
    "system": "Windows",
    # User
    "user name": "User Name",
    "username": "User Name",
    "user": "User Name",
    "login": "User Name",
    "user login": "User Name",
    "account": "User Name",
    "account name": "User Name",
    "computer user": "User Name",
    "current user": "User Name",
    "user account": "User Name",
    # PC name
    "pc name": "PC Name",
    "pcname": "PC Name",
    "computer name": "PC Name",
    "computername": "PC Name",
    "computer": "PC Name",
    "hostname": "PC Name",
    "host name": "PC Name",
    "host": "PC Name",
    "machine name": "PC Name",
    "machine": "PC Name",
    "device name": "PC Name",
    "device": "PC Name",
    "pc": "PC Name",
    "workstation": "PC Name",
    "netbios name": "PC Name",
    "netbios": "PC Name",
    # Display
    "display resolution": "Display Resolution",
    "screen resolution": "Display Resolution",
    "resolution": "Display Resolution",
    "display": "Display Resolution",
    "screen": "Display Resolution",
    "monitor": "Display Resolution",
    "display size": "Display Resolution",
    "screen size": "Display Resolution",
    "screensize": "Display Resolution",
    # Security
    "antivirus": "Antivirus",
    "anti virus": "Antivirus",
    "av": "Antivirus",
    "antivirus software": "Antivirus",
    "security software": "Antivirus",
    "defender": "Antivirus",
    # Hardware
    "processor": "Processor",
    "cpu": "Processor",
    "processor name": "Processor",
    "processor model": "Processor",
    "cpu name": "Processor",
    "cpu model": "Processor",
    "cpu info": "Processor",
    "cores": "Cores",
    "core count": "Cores",
    "cpu cores": "Cores",
    "number of cores": "Cores",
    "physical cores": "Cores",
    "threads": "Threads",
    "cpu threads": "Threads",
    "number of threads": "Threads",
    "logical cores": "Threads",
    "logical processors": "Threads",
    "ram": "RAM",
    "memory": "RAM",
    "ram size": "RAM",
    "ram total": "RAM",
    "memory size": "RAM",
    "total memory": "RAM",
    "installed memory": "RAM",
    "installed ram": "RAM",
    "physical memory": "RAM",
    "videocard": "VideoCard",
    "video card": "VideoCard",
    "video card name": "VideoCard",
    "gpu": "VideoCard",
    "gpu name": "VideoCard",
    "graphics": "VideoCard",
    "graphics card": "VideoCard",
    "video": "VideoCard",
    "motherboard": "Motherboard",
    "mainboard": "Motherboard",
    "baseboard": "Motherboard",
    "mobo": "Motherboard",
    "disk": "Disk",
    "disk size": "Disk",
    "hard drive": "Disk",
    "hdd": "Disk",
    "ssd": "Disk",
    "storage": "Disk",
    # Network / geo
    "ip": "IP",
    "ip address": "IP",
    "ipaddress": "IP",
    "ipv4": "IP",
    "public ip": "IP",
    "public ip address": "IP",
    "external ip": "IP",
    "country": "Country",
    "country code": "Country",
    "country name": "Country",
    "city": "City",
    "town": "City",
    # Misc
    "language": "Language",
    "user language": "Language",
    "userlanguage": "Language",
    "locale": "Language",
    "system language": "Language",
    "install date": "Install Date",
    "installation date": "Install Date",
    "os install date": "Install Date",
    "windows install date": "Install Date",
    "time zone": "Time Zone",
    "timezone": "Time Zone",
    "utc offset": "Time Zone",
}

# Directory names such as "DESKTOP-ABC_17.10.2026_20.33.25" or
# "2023-10-17_DESKTOP-ABC" embed the PC name next to the collection date.
_TRAILING_DATE_RE = re.compile(r"[_\-\s]+\d{1,4}[._\-/]\d{1,2}[._\-/]\d{2,4}.*$")
_LEADING_DATE_RE = re.compile(r"^\d{4}[._\-]\d{1,2}[._\-]\d{1,2}[_\-\s]*")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")




def find_leak_dirs(email):
    query = text("""
        SELECT DISTINCT analyzed_dir, leak_date
        FROM emails_to_send
        WHERE recipient_email LIKE :email
        ORDER BY leak_date DESC, analyzed_dir DESC
    """)

    with get_db_engine().begin() as conn:
        rows = conn.execute(query, {"email": email}).fetchall()

    return rows


# ---------------------------------------------------------------------------
# PC info discovery
# ---------------------------------------------------------------------------


def _is_info_file_name(name: str) -> bool:
    lowered = name.lower()
    return lowered in INFO_FILE_NAMES or lowered.endswith(INFO_FILE_SUFFIXES)


def find_info_files(directory):
    """Return the candidate PC-info files inside *directory* (shallow first)."""
    root = Path(directory)
    if not root.is_dir():
        return []

    found = []
    for current, dirs, files in os.walk(root):
        try:
            depth = len(Path(current).relative_to(root).parts)
        except ValueError:
            depth = 0
        if depth >= MAX_INFO_DEPTH:
            dirs[:] = []
        for name in files:
            if _is_info_file_name(name):
                path = Path(current) / name
                found.append((depth, path))

    found.sort(key=lambda item: (item[0], item[1].name.lower()))
    return [path for _, path in found]


def _read_text(path: Path):
    """Read a text file trying the encodings typically used by stealers."""
    try:
        if path.stat().st_size > MAX_INFO_FILE_SIZE:
            return None
        raw = path.read_bytes()
    except OSError:
        return None

    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ["utf-16"]
    else:
        encodings = ["utf-8-sig", "cp1252", "latin-1"]

    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def _normalize_label(label: str) -> str:
    label = label.strip().strip("\"'").rstrip(":").strip()
    label = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", label)
    label = re.sub(r"[_\-.]+", " ", label)
    return re.sub(r"\s+", " ", label).strip().lower()


def canonical_label(label: str):
    """Map a raw label to its canonical display name (or None if unknown)."""
    return LABEL_ALIASES.get(_normalize_label(label))


def parse_text_info(content: str):
    """Parse "Label: value" style info files.

    Returns (fields, extras) where *fields* maps canonical labels to values and
    *extras* is a list of (label, value) for labels we don't know about.
    """
    fields = {}
    extras = []
    seen_extras = set()

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "//", ";", "[")):
            continue

        match = re.match(r"^([^:=]{1,60}?)\s*[:=]\s*(.+)$", line)
        if not match:
            continue

        label = match.group(1).strip()
        value = match.group(2).strip()
        if not label or not value:
            continue

        canonical = canonical_label(label)
        if canonical:
            if canonical not in fields:
                fields[canonical] = value
            continue

        key = label.lower()
        if key in seen_extras:
            continue
        seen_extras.add(key)
        extras.append((label, value))

    return fields, extras


def _flatten_json(data, prefix=""):
    items = []
    if isinstance(data, dict):
        for key, value in data.items():
            full_key = f"{prefix}{key}"
            if isinstance(value, (dict, list)):
                items.extend(_flatten_json(value, f"{full_key}."))
            elif value is not None and str(value).strip():
                # Stealers write 0 when the WMI query failed (e.g. RAM and
                # CpuCores), which would otherwise look like real hardware.
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and value == 0
                ):
                    continue
                items.append((full_key, str(value).strip()))
    elif isinstance(data, list):
        scalars = [
            str(item).strip()
            for item in data
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
        if scalars:
            items.append((prefix.rstrip("."), ", ".join(scalars)))
        else:
            for index, value in enumerate(data):
                items.extend(_flatten_json(value, f"{prefix}{index}."))
    return items


def parse_json_info(content: str):
    """Parse JSON style info files (pc_info.json and friends)."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return {}, []

    fields = {}
    extras = []
    seen_extras = set()

    for key, value in _flatten_json(data):
        leaf = key.rsplit(".", 1)[-1]
        canonical = canonical_label(leaf)
        if canonical:
            if canonical not in fields:
                fields[canonical] = value
            continue

        key_lower = key.lower()
        if key_lower in seen_extras:
            continue
        seen_extras.add(key_lower)
        extras.append((key, value))

    return fields, extras


def parse_info_file(path: Path):
    content = _read_text(path)
    if not content:
        return {}, []

    if path.suffix.lower() == ".json" or content.lstrip().startswith("{"):
        return parse_json_info(content)
    return parse_text_info(content)


def load_pc_info(directory):
    """Return (fields, extras, sources) merging all the info files found.

    The file with the most recognised fields wins on conflicts; the remaining
    files only fill the gaps (e.g. RedLine keeps the install date and the
    country in UserInformation.txt while System.txt holds the hardware).
    """
    parsed = []
    for path in find_info_files(directory):
        fields, extras = parse_info_file(path)
        if not fields and not extras:
            continue
        parsed.append((len(fields) * 2 + len(extras), path, fields, extras))

    if not parsed:
        return {}, [], []

    parsed.sort(key=lambda item: item[0], reverse=True)

    merged_fields = {}
    extras = []
    seen_extras = set()
    sources = []

    for _score, path, fields, file_extras in parsed:
        for label, value in fields.items():
            merged_fields.setdefault(label, value)
        for label, value in file_extras:
            key = label.lower()
            if key in seen_extras:
                continue
            seen_extras.add(key)
            extras.append((label, value))
        sources.append(path)

    return merged_fields, extras, sources


def guess_pc_name(directory):
    """Best-effort PC name extracted from the leak directory name."""
    name = Path(directory).name.strip()
    if not name:
        return None

    candidate = _LEADING_DATE_RE.sub("", name)
    candidate = _TRAILING_DATE_RE.sub("", candidate).strip(" _-.")

    if not candidate or not _HOSTNAME_RE.match(candidate):
        return None
    if candidate.isdigit():
        return None
    return candidate


def _display_value(value, limit=MAX_DISPLAY_VALUE):
    """Collapse whitespace and shorten over-long values for display."""
    text_value = re.sub(r"\s+", " ", str(value)).strip()
    if len(text_value) <= limit:
        return text_value
    return text_value[: limit - 1].rstrip() + "…"


def render_pc_info(fields, extras, directory=None):
    """Build the display block for the collected PC information."""
    fields = dict(fields)

    if (fields or extras) and "PC Name" not in fields and directory:
        guessed = guess_pc_name(directory)
        if guessed:
            fields["PC Name"] = guessed

    groups = []
    identity = [
        f"{label}: {_display_value(fields[label])}"
        for label in IDENTITY_FIELDS
        if label in fields
    ]
    if identity:
        groups.append(identity)

    system = [
        f"{label}: {_display_value(fields[label])}"
        for label in SYSTEM_FIELDS
        if label in fields
    ]
    if system:
        groups.append(system)

    hardware = [
        f"{label}: {_display_value(fields[label])}"
        for label in HARDWARE_FIELDS
        if label in fields
    ]
    if hardware:
        groups.append(["[Hardware]", *hardware])

    known = set(IDENTITY_FIELDS) | set(SYSTEM_FIELDS) | set(HARDWARE_FIELDS)
    remaining = [(label, value) for label, value in extras if label not in known]
    remaining += [(label, value) for label, value in fields.items() if label not in known]
    if remaining:
        groups.append(
            [
                "[Other]",
                *[f"{label}: {_display_value(value)}" for label, value in remaining],
            ]
        )

    return "\n\n".join("\n".join(group) for group in groups)


def summarize_pc_info(directory):
    """One-line summary used in the interactive picker."""
    fields, _extras, _sources = load_pc_info(directory)
    if not fields:
        return ""

    parts = []
    for label in ("PC Name", "User Name", "Windows", "Date"):
        if label in fields:
            parts.append(_display_value(fields[label], 40))
    if "PC Name" not in fields:
        guessed = guess_pc_name(directory)
        if guessed:
            parts.insert(0, guessed)
    return " | ".join(parts)


def _format_sources(sources, directory):
    names = []
    for path in sources:
        try:
            names.append(str(path.relative_to(directory)))
        except ValueError:
            names.append(str(path))
    if len(names) > 3:
        return ", ".join(names[:3]) + ", ..."
    return ", ".join(names)


def print_pc_info(directory, stream=None):
    """Print the PC info block for *directory* (empty if nothing was found)."""
    stream = stream or sys.stderr
    fields, extras, sources = load_pc_info(directory)

    block = render_pc_info(fields, extras, directory)
    if not block or (not fields and not extras):
        print("PC info: no machine information file found", file=stream)
        return

    if sources:
        print(f"PC info ({_format_sources(sources, directory)}):", file=stream)
    else:
        print("PC info:", file=stream)
    print(block, file=stream)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv):
    options = {"email": None, "show_info": True, "info_only": False, "help": False}
    positional = []

    for arg in argv:
        if arg in ("-h", "--help"):
            options["help"] = True
        elif arg == "--no-info":
            options["show_info"] = False
        elif arg in ("-i", "--info-only"):
            options["info_only"] = True
            options["show_info"] = True
        else:
            positional.append(arg)

    if positional:
        options["email"] = positional[0].strip()
    return options


def main():
    setup_logging()
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

    if not rows:
        print(f"No leak directories found for: {email}", file=sys.stderr)
        sys.exit(1)

    if len(rows) == 1:
        directory = rows[0][0]
        if options["info_only"]:
            print_pc_info(directory, stream=sys.stdout)
            return
        if options["show_info"]:
            print_pc_info(directory)
        print(directory)
        return

    # Multiple results: interactive picker
    print(f"Found {len(rows)} leak directories for {email}:\n", file=sys.stderr)
    with_summary = len(rows) <= 20
    for i, (dir_path, leak_date) in enumerate(rows, 1):
        date_str = f" (leak: {leak_date})" if leak_date else ""
        print(f"  {i}. {dir_path}{date_str}", file=sys.stderr)
        if with_summary:
            summary = summarize_pc_info(dir_path)
            if summary:
                print(f"     {summary}", file=sys.stderr)

    print(file=sys.stderr)
    sys.stderr.write("Select number (or Enter to cancel): ")
    sys.stderr.flush()
    try:
        choice = sys.stdin.readline().strip()
    except KeyboardInterrupt:
        sys.exit(1)

    if not choice or not choice.isdigit() or int(choice) < 1 or int(choice) > len(rows):
        sys.exit(1)

    directory = rows[int(choice) - 1][0]

    if options["info_only"]:
        print_pc_info(directory, stream=sys.stdout)
        return
    if options["show_info"]:
        print_pc_info(directory)
    print(directory)


if __name__ == "__main__":
    main()
