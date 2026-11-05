#!/usr/bin/env python3
"""
Show per-channel Telegram stats: channel name, ID, date of last received
message, date of last processed download, and total processed files.

Usage:
    python channels_stats.py          # CLI table output
    python channels_stats.py --tui    # Interactive TUI mode
"""

import argparse
import curses
import logging
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from sqlalchemy import text
from telethon.sync import TelegramClient
from telethon.tl.types import Channel, ChannelForbidden

import config
from utils import get_db_engine, setup_logging

logger = logging.getLogger(__name__)

DATE_FMT = "%Y-%m-%d %H:%M"


def display_width(s: str) -> int:
    """Calculate display width of string (wide chars count as 2)."""
    return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in s)


def truncate_to_width(s: str, width: int) -> str:
    """Truncate string to display width, preserving wide chars."""
    result = []
    current = 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
        if current + cw > width:
            break
        result.append(c)
        current += cw
    return ''.join(result)


@dataclass
class ChannelRow:
    name: str
    cid: str
    status: str
    restriction_detail: str
    last_msg: Optional[datetime]
    last_processed: Optional[datetime]
    total: int
    last_msg_id: int = 0
    channel_link: str = "NO LINK"
    selected: bool = False


class SortColumn(Enum):
    NAME = 0
    ID = 1
    STATUS = 2
    LAST_MSG = 3
    LAST_PROC = 4
    LAST_MSG_ID = 5
    TOTAL = 6


COLUMNS = [
    ("Channel Name", 36),
    ("ID", 20),
    ("Status", 10),
    ("Last Msg", 16),
    ("Last Proc", 16),
    ("Last Msg ID", 14),
    ("Done", 6),
]

COLUMN_KEYS = [c[0].lower().replace(" ", "_") for c in COLUMNS]


def fmt_date(dt: Optional[datetime]) -> str:
    if dt is None:
        return "-"
    return dt.strftime(DATE_FMT)


def fetch_channel_data() -> List[ChannelRow]:
    """Fetch all channel data from Telegram and DB."""
    engine = get_db_engine()
    client = TelegramClient("CHANNELS_STATS", config.API_ID, config.API_HASH)
    client.start(config.API_PHONE)

    rows = []

    for dialog in client.iter_dialogs():
        ent = dialog.entity
        if not (hasattr(ent, "broadcast") and ent.broadcast):
            continue

        channel = dialog.entity
        cid = str(channel.id)
        cname = channel.title or "Unknown"
        # Build channel link from username
        username = getattr(channel, "username", None)
        if username:
            channel_link = f"https://t.me/{username}"
        else:
            channel_link = "NO LINK"
        last_msg = dialog.message.date if dialog.message else None

        if isinstance(channel, ChannelForbidden):
            status = "BANNED"
            restriction_detail = ""
        elif isinstance(channel, Channel):
            if channel.restricted:
                status = "RESTRICTED"
                reasons = getattr(channel, "restriction_reason", [])
                if reasons:
                    restriction_detail = "; ".join(
                        f"[{r.platform}] {r.reason}: {r.text}" for r in reasons
                    )
                else:
                    restriction_detail = "No reason provided"
            elif channel.left:
                status = "LEFT"
                restriction_detail = ""
            else:
                status = "OK"
                restriction_detail = ""
        else:
            status = "?"
            restriction_detail = ""

        # channel_state uses -100 prefix (Telethon chat_id format), but dialog.entity.id is raw numeric
        state_cid = f"-100{cid}"

        with engine.begin() as conn:
            stats = conn.execute(
                text("""
                    SELECT 
                        cs.updated_at AS last_activity,
                        cs.last_id AS last_processed_msg_id,
                        COUNT(CASE WHEN df.status IN 
                            ('DOWNLOADED','UNCOMPRESSED','WALKED_DIR',
                             'PROCESSED','PROCESSED_NOMAIL','PROCESSED_EMAILS_ALREADY_SENT')
                            THEN 1 END) AS total_ok,
                        COUNT(CASE WHEN df.status IN 
                            ('WRONGPASSWORD','OTHERERRORS','ERROR_WALKED_DIR')
                            THEN 1 END) AS total_errors,
                        COUNT(*) AS total_all
                    FROM channel_state cs
                    LEFT JOIN downloaded_files df ON df.channel_id = cs.channel_id
                    WHERE cs.channel_id = :cid
                    GROUP BY cs.channel_id
                """),
                {"cid": state_cid},
            ).fetchone()

            if stats:
                last_processed = stats[0]      # channel_state.updated_at
                last_msg_id = stats[1]         # channel_state.last_id
                total_ok = stats[2]            # successful files
                total_errors = stats[3]        # error files
                total_all = stats[4]           # all files
            else:
                # Fallback: try without prefix, then downloaded_files
                fallback = conn.execute(
                    text("""
                        SELECT 
                            MAX(downloaded_at),
                            COUNT(CASE WHEN status IN 
                                ('DOWNLOADED','UNCOMPRESSED','WALKED_DIR',
                                 'PROCESSED','PROCESSED_NOMAIL','PROCESSED_EMAILS_ALREADY_SENT')
                                THEN 1 END),
                            COUNT(CASE WHEN status IN 
                                ('WRONGPASSWORD','OTHERERRORS','ERROR_WALKED_DIR')
                                THEN 1 END),
                            COUNT(*)
                        FROM downloaded_files
                        WHERE channel_id IN (:cid, :state_cid)
                    """),
                    {"cid": cid, "state_cid": state_cid},
                ).fetchone()
                last_processed = fallback[0] if fallback else None
                total_ok = fallback[1] if fallback else 0
                total_errors = fallback[2] if fallback else 0
                total_all = fallback[3] if fallback else 0
                last_msg_id = 0

            total = total_ok  # Use successful count as "Done"

        rows.append(ChannelRow(
            name=cname,
            cid=cid,
            status=status,
            restriction_detail=restriction_detail,
            last_msg=last_msg,
            last_processed=last_processed,
            total=total,
            last_msg_id=last_msg_id,
            channel_link=channel_link,
        ))

    client.disconnect()
    return rows


def sort_rows(rows: List[ChannelRow], column: SortColumn, reverse: bool) -> List[ChannelRow]:
    """Sort rows by the given column."""
    # Convert datetime to timestamp for safe comparison (handles both naive and aware)
    def dt_to_ts(dt: Optional[datetime]) -> float:
        if dt is None:
            return float('-inf')
        if dt.tzinfo is not None:
            return dt.timestamp()
        # Assume naive datetime is UTC
        return dt.replace(tzinfo=timezone.utc).timestamp()
    
    key_map = {
        SortColumn.NAME: lambda r: r.name.lower(),
        SortColumn.ID: lambda r: int(r.cid) if r.cid.isdigit() else 0,
        SortColumn.STATUS: lambda r: r.status,
        SortColumn.LAST_MSG: lambda r: dt_to_ts(r.last_msg),
        SortColumn.LAST_PROC: lambda r: dt_to_ts(r.last_processed),
        SortColumn.LAST_MSG_ID: lambda r: r.last_msg_id,
        SortColumn.TOTAL: lambda r: r.total,
    }
    return sorted(rows, key=key_map[column], reverse=reverse)


def draw_header(stdscr, cols: List[tuple], sort_col: SortColumn, reverse: bool, name_w: int):
    """Draw fixed header row with sort indicators."""
    h, w = stdscr.getmaxyx()
    header_y = 0
    
    # Build header line
    parts = []
    for i, (col_name, col_w) in enumerate(cols):
        if i == 0:
            actual_w = name_w
        else:
            actual_w = col_w
        
        indicator = ""
        if i == sort_col.value:
            indicator = " ▼" if reverse else " ▲"
        
        col_text = f"{col_name}{indicator}"
        parts.append(col_text[:actual_w].ljust(actual_w))
    
    header_line = "  ".join(parts)
    # Use color pair 1 for header (white on blue)
    stdscr.addstr(header_y, 0, header_line[:w-1], curses.color_pair(1) | curses.A_BOLD)
    
    # Separator line
    sep = "=" * min(len(header_line), w - 1)
    stdscr.addstr(header_y + 1, 0, sep, curses.A_DIM)


def draw_row(stdscr, y: int, row: ChannelRow, cols: List[tuple], name_w: int, 
             nav_idx: int, abs_idx: int, w: int, h: int):
    """Draw a single data row."""
    is_selected = row.selected
    is_current = (nav_idx == abs_idx)  # nav_idx = user's cursor position
    
    # Determine color pair
    if curses.has_colors():
        if is_selected and is_current:
            attr = curses.color_pair(5) | curses.A_BOLD  # white on cyan, bold
        elif is_selected:
            attr = curses.color_pair(4) | curses.A_BOLD  # white on magenta, bold
        elif is_current:
            attr = curses.color_pair(3)  # black on cyan
        else:
            attr = curses.A_NORMAL
    else:
        # Fallback without colors
        if is_selected and is_current:
            attr = curses.A_REVERSE | curses.A_BOLD
        elif is_selected:
            attr = curses.A_BOLD
        elif is_current:
            attr = curses.A_REVERSE
        else:
            attr = curses.A_NORMAL
    
    parts = []
    # Use display-aware truncation and padding for name column
    name_display = truncate_to_width(row.name, name_w)
    # Pad with spaces to reach display width
    name_padded = name_display + ' ' * (name_w - display_width(name_display))
    parts.append(name_padded)
    parts.append(row.cid[:20].ljust(20))
    parts.append(row.status[:10].ljust(10))
    parts.append(fmt_date(row.last_msg)[:16].ljust(16))
    parts.append(fmt_date(row.last_processed)[:16].ljust(16))
    msg_id_str = str(row.last_msg_id) if row.last_msg_id else "-"
    parts.append(msg_id_str[:14].rjust(14))
    parts.append(str(row.total)[:6].rjust(6))
    
    line = "  ".join(parts)
    stdscr.addstr(y, 0, line[:w-1], attr)
    
    # Draw restriction detail on next line if RESTRICTED
    if row.status == "RESTRICTED" and row.restriction_detail and y + 1 < h - 1:
        detail = f"  → {row.restriction_detail}"
        if curses.has_colors():
            stdscr.addstr(y + 1, 0, detail[:w-1], curses.color_pair(9) | curses.A_DIM)
        else:
            stdscr.addstr(y + 1, 0, detail[:w-1], curses.A_DIM)
    
    # Color the status column
    if curses.has_colors():
        status_x = name_w + 2  # After name + "  "
        if row.status in ("RESTRICTED", "BANNED"):
            stdscr.addstr(y, status_x, row.status[:10].ljust(10), curses.color_pair(6) | curses.A_BOLD)
        elif row.status == "OK":
            stdscr.addstr(y, status_x, row.status[:10].ljust(10), curses.color_pair(7))
        elif row.status == "LEFT":
            stdscr.addstr(y, status_x, row.status[:10].ljust(10), curses.color_pair(8))


def draw_footer(stdscr, h: int, w: int, selected_count: int, total_count: int):
    """Draw footer with key hints and status."""
    footer_y = h - 1
    status = f" Selected: {selected_count}/{total_count}  "
    hints = " ↑/↓:nav  Space:select  s:sort  r:reverse  d:delete  l:leave  q:quit "
    line = status + hints
    if curses.has_colors():
        stdscr.addstr(footer_y, 0, line[:w-1], curses.color_pair(2))
    else:
        stdscr.addstr(footer_y, 0, line[:w-1], curses.A_REVERSE)


def run_tui(stdscr, rows: List[ChannelRow]):
    """Main TUI loop."""
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.nodelay(False)
    
    # Initialize colors - use explicit pairs that work on light/dark backgrounds
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            default_bg = -1
        except curses.error:
            default_bg = curses.COLOR_BLACK
        
        # Color pairs: (fg, bg)
        # 1: Header (bold white on blue)
        # 2: Footer (white on blue)
        # 3: Current row highlight (black on cyan)
        # 4: Selected row (bold white on magenta)
        # 5: Selected + Current (bold white on dark cyan)
        # 6: RESTRICTED/BANNED status (red)
        # 7: OK status (green)
        # 8: LEFT status (yellow)
        # 9: Restriction detail (dim)
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(4, curses.COLOR_WHITE, curses.COLOR_MAGENTA)
        curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_CYAN)
        curses.init_pair(6, curses.COLOR_RED, default_bg)
        curses.init_pair(7, curses.COLOR_GREEN, default_bg)
        curses.init_pair(8, curses.COLOR_YELLOW, default_bg)
        curses.init_pair(9, curses.COLOR_WHITE, default_bg)  # dim uses A_DIM
    
    h, w = stdscr.getmaxyx()
    data_start_y = 2  # Header at 0, separator at 1
    max_data_rows = h - 3  # Leave room for footer
    
    # Calculate name column width
    name_w = max((display_width(r.name) for r in rows), default=36)
    name_w = max(name_w, 36)
    name_w = min(name_w, w - 76)  # Leave space for other columns (ID+Status+LastMsg+LastProc+MsgID+Done = ~82)
    
    sort_col = SortColumn.LAST_PROC
    reverse_sort = True
    current_idx = 0
    scroll_offset = 0
    
    while True:
        h, w = stdscr.getmaxyx()
        max_data_rows = h - 3
        stdscr.erase()
        
        # Draw header
        draw_header(stdscr, COLUMNS, sort_col, reverse_sort, name_w)
        
        # Draw visible rows
        visible_rows = rows[scroll_offset:scroll_offset + max_data_rows]
        y = data_start_y
        for i, row in enumerate(visible_rows):
            if y >= h - 1:
                break
            draw_row(stdscr, y, row, COLUMNS, name_w, 
                     current_idx, scroll_offset + i, w, h)
            # Extra line for restriction detail
            if row.status == "RESTRICTED" and row.restriction_detail:
                y += 1
            y += 1
        
        # Draw footer
        selected_count = sum(1 for r in rows if r.selected)
        draw_footer(stdscr, h, w, selected_count, len(rows))
        
        stdscr.refresh()
        
        key = stdscr.getch()
        
        if key == ord('q') or key == 27:  # q or ESC
            break
        elif key == curses.KEY_UP:
            if current_idx > 0:
                current_idx -= 1
                if current_idx < scroll_offset:
                    scroll_offset = current_idx
        elif key == curses.KEY_DOWN:
            if current_idx < len(rows) - 1:
                current_idx += 1
                if current_idx >= scroll_offset + max_data_rows:
                    scroll_offset = current_idx - max_data_rows + 1
        elif key == ord(' '):  # Space - toggle selection
            if rows:
                rows[current_idx].selected = not rows[current_idx].selected
        elif key == ord('a'):  # Select all
            for r in rows:
                r.selected = True
        elif key == ord('n'):  # Select none
            for r in rows:
                r.selected = False
        elif key == ord('s'):  # Sort by column (cycle)
            sort_col = SortColumn((sort_col.value + 1) % len(SortColumn))
            rows = sort_rows(rows, sort_col, reverse_sort)
            current_idx = 0
            scroll_offset = 0
        elif key == ord('r'):  # Reverse sort
            reverse_sort = not reverse_sort
            rows = sort_rows(rows, sort_col, reverse_sort)
            current_idx = 0
            scroll_offset = 0
        elif key == ord('d') or key == ord('D'):  # Delete selected from DB
            selected_rows = [r for r in rows if r.selected]
            if selected_rows:
                confirm = show_confirm_dialog(stdscr, h, w,
                    f"Delete {len(selected_rows)} selected channel(s) from database?")
                if confirm:
                    engine = get_db_engine()
                    with engine.begin() as conn:
                        for r in selected_rows:
                            conn.execute(
                                text("DELETE FROM channel_list WHERE channel_id = :cid"),
                                {"cid": r.cid}
                            )
                    # Remove from local list
                    rows = [r for r in rows if not r.selected]
                    current_idx = min(current_idx, len(rows) - 1) if rows else 0
                    scroll_offset = min(scroll_offset, current_idx)
        elif key == ord('l') or key == ord('L'):  # Leave selected channels (Telegram)
            selected_rows = [r for r in rows if r.selected]
            if selected_rows:
                confirm = show_confirm_dialog(stdscr, h, w,
                    f"Leave {len(selected_rows)} selected channel(s) on Telegram?")
                if confirm:
                    client = TelegramClient("CHANNELS_STATS_LEAVE", config.API_ID, config.API_HASH)
                    client.start(config.API_PHONE)
                    engine = get_db_engine()
                    for r in selected_rows:
                        try:
                            client.delete_dialog(int(r.cid))
                            with engine.begin() as conn:
                                conn.execute(
                                    text("""
                                        UPDATE channel_list
                                        SET status = 'LEFT', date_removed = NOW()
                                        WHERE channel_id = :cid AND status = 'ACTIVE'
                                    """),
                                    {"cid": r.cid}
                                )
                            r.status = "LEFT"
                            r.selected = False
                        except Exception as e:
                            logger.error(f"Failed to leave {r.name}: {e}")
                    client.disconnect()
        elif key == curses.KEY_RESIZE:
            # Recalculate on resize
            name_w = max((display_width(r.name) for r in rows), default=36)
            name_w = max(name_w, 36)
            name_w = min(name_w, w - 76)
    
    return rows


def show_confirm_dialog(stdscr, h: int, w: int, message: str) -> bool:
    """Show a confirmation dialog."""
    dialog_h = 7
    dialog_w = min(max(len(message) + 6, 50), w - 4)
    start_y = (h - dialog_h) // 2
    start_x = (w - dialog_w) // 2
    
    dialog = curses.newwin(dialog_h, dialog_w, start_y, start_x)
    dialog.keypad(True)
    if curses.has_colors():
        dialog.bkgd(' ', curses.color_pair(2))
    dialog.box()
    
    # Draw message (word wrap)
    words = message.split()
    lines = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 <= dialog_w - 4:
            current += (" " + word) if current else word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    
    for i, line in enumerate(lines):
        dialog.addstr(1 + i, 2, line[:dialog_w-4])
    
    # Buttons
    btn_y = dialog_h - 2
    if curses.has_colors():
        dialog.addstr(btn_y, 2, "[Y] Yes  [N] No", curses.color_pair(3))
    else:
        dialog.addstr(btn_y, 2, "[Y] Yes  [N] No", curses.A_REVERSE)
    dialog.refresh()
    
    while True:
        key = dialog.getch()
        if key in (ord('y'), ord('Y')):
            return True
        elif key in (ord('n'), ord('N'), 27):  # n, N, ESC
            return False


def print_cli_table(rows: List[ChannelRow]):
    """Original CLI table output."""
    if not rows:
        print("\nNo subscribed channels found.")
        return
    
    name_w = max(display_width(r.name) for r in rows)
    name_w = max(name_w, 36)
    link_w = 32  # Width for channel link column
    sep = "=" * (name_w + link_w + 100)
    
    print(f"\n{sep}")
    print(
        f"{'Channel Name':<{name_w}}  {'ID':<20}  {'Link':<{link_w}}  {'Status':<10}  "
        f"{'Last Msg':<16}  {'Last Proc':<16}  {'Last Msg ID':>14}  {'Done':>6}"
    )
    print(sep)
    
    for r in rows:
        name_truncated = truncate_to_width(r.name, name_w)
        link_display = truncate_to_width(r.channel_link, link_w)
        msg_id_str = str(r.last_msg_id) if r.last_msg_id else "-"
        print(
            f"{name_truncated:<{name_w}}  {r.cid:<20}  {link_display:<{link_w}}  {r.status:<10}  "
            f"{fmt_date(r.last_msg):<16}  "
            f"{fmt_date(r.last_processed):<16}  {msg_id_str:>14}  {r.total:>6}"
        )
        if r.status == "RESTRICTED" and r.restriction_detail:
            print(f"  {'':<{name_w}}  {'':<20}  {'':<{link_w}}  {'':<10}  → {r.restriction_detail}")
    
    print(sep)
    print(f"\nTotal channels: {len(rows)}\n")
    
    # RESTRICTED prompt (original behavior)
    restricted = [r for r in rows if r.status == "RESTRICTED"]
    if restricted:
        print(f"⚠️  Found {len(restricted)} RESTRICTED channel(s).")
        try:
            prompt = "Leave RESTRICTED channels? (y/N): "
            answer = input(prompt).strip().lower()
        except EOFError:
            answer = "n"
        
        if answer == "y":
            client = TelegramClient("CHANNELS_STATS", config.API_ID, config.API_HASH)
            client.start(config.API_PHONE)
            engine = get_db_engine()
            for r in restricted:
                try:
                    client.delete_dialog(int(r.cid))
                    with engine.begin() as conn:
                        conn.execute(
                            text("""
                                UPDATE channel_list
                                SET status = 'LEFT', date_removed = NOW()
                                WHERE channel_id = :cid AND status = 'ACTIVE'
                            """),
                            {"cid": r.cid},
                        )
                    print(f"  ✓ Left: {r.name} ({r.cid})")
                except Exception as e:
                    print(f"  ✗ Failed to leave {r.name} ({r.cid}): {e}")
            print(f"\nDone. Left {len(restricted)} channel(s).\n")
            client.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Telegram channel statistics")
    parser.add_argument("--tui", action="store_true", help="Run in interactive TUI mode")
    parser.add_argument("--with-link", action="store_true", help="Show only channels with a valid public link (username)")
    parser.add_argument("--stale", type=int, metavar="MONTHS", help="Show only channels with Last Proc older than N months (default: 6)")
    parser.add_argument("--no-proc", action="store_true", help="Show only channels never processed (no Last Proc)")
    parser.add_argument("--restricted", action="store_true", help="Show only RESTRICTED channels")
    parser.add_argument("--unsubscribe", action="store_true", help="Leave filtered channels on Telegram and mark as LEFT in database")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt for --unsubscribe")
    args = parser.parse_args()
    
    setup_logging()
    rows = fetch_channel_data()
    
    if not rows:
        print("\nNo subscribed channels found.")
        return
    
    # Filter: only channels with valid link
    if args.with_link:
        rows = [r for r in rows if r.channel_link != "NO LINK"]
    
    # Filter: last_proc older than N months
    if args.stale is not None:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.stale * 30)
        # Make cutoff naive for comparison with MySQL naive UTC timestamps
        cutoff = cutoff.replace(tzinfo=None)
        rows = [r for r in rows if r.last_processed and r.last_processed < cutoff]
    
    # Filter: never processed (no Last Proc)
    if args.no_proc:
        rows = [r for r in rows if r.last_processed is None]
    
    # Filter: only RESTRICTED channels
    if args.restricted:
        rows = [r for r in rows if r.status == "RESTRICTED"]
    
    # Default sort: Last Proc descending (most recently processed first)
    rows = sort_rows(rows, SortColumn.LAST_PROC, True)
    
    if args.unsubscribe:
        if not rows:
            print("No channels match the filters.")
            return
        
        print(f"\nChannels to unsubscribe ({len(rows)}):")
        for r in rows:
            print(f"  {r.name} ({r.cid}) - Last Proc: {fmt_date(r.last_processed)}")
        
        if not args.yes:
            try:
                confirm = input(f"\nLeave these {len(rows)} channel(s)? (y/N): ").strip().lower()
            except EOFError:
                confirm = "n"
            if confirm != "y":
                print("Aborted.")
                return
        
        client = TelegramClient("CHANNELS_STATS_UNSUB", config.API_ID, config.API_HASH)
        client.start(config.API_PHONE)
        engine = get_db_engine()
        
        success = 0
        failed = 0
        for r in rows:
            try:
                # Use -100 prefix for channel entity (PeerChannel)
                state_cid = int(f"-100{r.cid}")
                client.delete_dialog(state_cid)
                with engine.begin() as conn:
                    conn.execute(
                        text("""
                            UPDATE channel_list
                            SET status = 'LEFT', date_removed = NOW()
                            WHERE channel_id = :cid AND status = 'ACTIVE'
                        """),
                        {"cid": r.cid},
                    )
                print(f"  ✓ Left: {r.name} ({r.cid})")
                success += 1
            except Exception as e:
                print(f"  ✗ Failed to leave {r.name} ({r.cid}): {e}")
                failed += 1
        
        client.disconnect()
        print(f"\nDone. Success: {success}, Failed: {failed}")
        return
    
    if args.tui:
        try:
            curses.wrapper(run_tui, rows)
        except KeyboardInterrupt:
            pass
    else:
        print_cli_table(rows)


if __name__ == "__main__":
    main()