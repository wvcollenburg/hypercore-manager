#!/usr/bin/env python3
"""HyperCore Manager — interactive installer (nmtui-style).

Walks the operator through every setting in .env, explaining what each one
does, why the default is what it is, and when to change it. Generates strong
passwords/tokens on demand. Detects an existing .env and pre-fills it.

Run from the cloned repo root:

    python3 install.py

Linux/macOS only — uses the curses module from the Python stdlib.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

try:
    import curses
except ImportError:
    if sys.platform == "win32":
        print("The 'curses' module is not available on Windows by default.", file=sys.stderr)
        print("Install the windows-curses shim, then re-run:", file=sys.stderr)
        print("    pip install windows-curses", file=sys.stderr)
    else:
        print("The 'curses' module is missing from your Python install.", file=sys.stderr)
    sys.exit(1)

from installer.core import (
    EASY_PROFILES,
    EASY_REQUIRED_KEYS,
    ENV_PATH,
    Field,
    PLACEHOLDERS,
    Section,
    TEMPLATE_PATH,
    apply_easy_autogen,
    validate_timezone,
    timezone_regions,
    timezone_cities,
    autofill_paths,
    build_schema,
    collect_path_warnings,
    collect_validation_errors,
    extract_host_ip,
    gen_password,
    gen_token,
    is_section_gated_off,
    looks_like_host,
    normalize_sc_host,
    parse_env,
    path_exists,
    section_status,
    seed_default_values,
    validate_value,
    write_env,
)


# ============================================================================
# constants — UI-only, schema/data lives in installer.core
# ============================================================================

MIN_COLS = 80
MIN_ROWS = 24

# color pair IDs, initialised in init_colors
C_DEFAULT = 1
C_TITLE = 2
C_SELECTED = 3
C_HELP = 4
C_OK = 5
C_WARN = 6
C_ERR = 7
C_DIM = 8
C_BORDER = 9

# ============================================================================
# environment probes
# ============================================================================

def detect_compose() -> tuple[str, str]:
    """Detect docker compose CLI on PATH.

    Returns (status, version_str) where status is one of:
      - 'v2'      : `docker compose` plugin is available (preferred)
      - 'v1_only' : only the legacy python `docker-compose` (1.x) was found
      - 'none'    : neither was found

    Both probes are version queries that don't contact the docker daemon, so
    the result is independent of whether dockerd is running.
    """
    try:
        r = subprocess.run(
            ['docker', 'compose', 'version', '--short'],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return ('v2', r.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    try:
        r = subprocess.run(
            ['docker-compose', '--version'],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return ('v1_only', r.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    return ('none', '')


# ============================================================================
# curses helpers
# ============================================================================

def init_colors() -> None:
    curses.start_color()
    # try to use the terminal's default background; PDCurses on Windows may
    # not support this, so fall back to an explicit black background
    bg = -1
    try:
        curses.use_default_colors()
    except curses.error:
        bg = curses.COLOR_BLACK
    dim_fg = 8 if curses.COLORS >= 16 else curses.COLOR_WHITE
    curses.init_pair(C_DEFAULT, curses.COLOR_WHITE, bg)
    curses.init_pair(C_TITLE, curses.COLOR_WHITE, curses.COLOR_BLUE)
    curses.init_pair(C_SELECTED, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_HELP, curses.COLOR_CYAN, bg)
    curses.init_pair(C_OK, curses.COLOR_GREEN, bg)
    curses.init_pair(C_WARN, curses.COLOR_YELLOW, bg)
    curses.init_pair(C_ERR, curses.COLOR_RED, bg)
    curses.init_pair(C_DIM, dim_fg, bg)
    curses.init_pair(C_BORDER, curses.COLOR_CYAN, bg)


def addstr_safe(win, y: int, x: int, s: str, attr: int = 0) -> None:
    try:
        max_y, max_x = win.getmaxyx()
        if y < 0 or y >= max_y:
            return
        if x < 0:
            return
        s = s[: max(0, max_x - x - 1)]
        win.addstr(y, x, s, attr)
    except curses.error:
        pass


def draw_box(win, y: int, x: int, h: int, w: int, title: str = "") -> None:
    attr = curses.color_pair(C_BORDER)
    try:
        for i in range(w):
            win.addch(y, x + i, curses.ACS_HLINE, attr)
            win.addch(y + h - 1, x + i, curses.ACS_HLINE, attr)
        for i in range(h):
            win.addch(y + i, x, curses.ACS_VLINE, attr)
            win.addch(y + i, x + w - 1, curses.ACS_VLINE, attr)
        win.addch(y, x, curses.ACS_ULCORNER, attr)
        win.addch(y, x + w - 1, curses.ACS_URCORNER, attr)
        win.addch(y + h - 1, x, curses.ACS_LLCORNER, attr)
        win.addch(y + h - 1, x + w - 1, curses.ACS_LRCORNER, attr)
    except curses.error:
        pass
    if title:
        addstr_safe(win, y, x + 2, f" {title} ", curses.color_pair(C_BORDER) | curses.A_BOLD)


def draw_titlebar(stdscr, text: str) -> None:
    h, w = stdscr.getmaxyx()
    bar = " " * max(0, w - 1)
    try:
        stdscr.addstr(0, 0, bar, curses.color_pair(C_TITLE))
    except curses.error:
        pass
    addstr_safe(stdscr, 0, 2, text, curses.color_pair(C_TITLE) | curses.A_BOLD)


def draw_statusbar(stdscr, hints: str) -> None:
    h, w = stdscr.getmaxyx()
    bar = " " * max(0, w - 1)
    try:
        stdscr.addstr(h - 1, 0, bar, curses.color_pair(C_TITLE))
    except curses.error:
        pass
    addstr_safe(stdscr, h - 1, 2, hints, curses.color_pair(C_TITLE))


def wrap_text(text: str, width: int) -> list[str]:
    """Word-wrap to width."""
    if width <= 0:
        return []
    out: list[str] = []
    for paragraph in text.splitlines() or [text]:
        if not paragraph:
            out.append("")
            continue
        words = paragraph.split()
        line = ""
        for w in words:
            if not line:
                line = w
            elif len(line) + 1 + len(w) <= width:
                line += " " + w
            else:
                out.append(line)
                line = w
        if line:
            out.append(line)
    return out


# ============================================================================
# modal dialogs
# ============================================================================

def message_box(stdscr, title: str, lines: list[str], color_pair: int = C_DEFAULT) -> None:
    h, w = stdscr.getmaxyx()
    body_w = min(70, w - 4)
    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(wrap_text(line, body_w - 4) or [""])
    box_h = len(wrapped) + 4
    box_w = body_w
    y0 = max(1, (h - box_h) // 2)
    x0 = max(1, (w - box_w) // 2)
    win = curses.newwin(box_h, box_w, y0, x0)
    win.bkgd(" ", curses.color_pair(C_DEFAULT))
    draw_box(win, 0, 0, box_h, box_w, title)
    for i, line in enumerate(wrapped):
        addstr_safe(win, 1 + i, 2, line, curses.color_pair(color_pair))
    addstr_safe(win, box_h - 2, 2, "[ Press any key ]", curses.color_pair(C_DIM))
    win.refresh()
    win.getch()


def confirm_box(stdscr, title: str, lines: list[str], default_yes: bool = True) -> bool:
    h, w = stdscr.getmaxyx()
    body_w = min(70, w - 4)
    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(wrap_text(line, body_w - 4) or [""])
    box_h = len(wrapped) + 5
    box_w = body_w
    y0 = max(1, (h - box_h) // 2)
    x0 = max(1, (w - box_w) // 2)
    win = curses.newwin(box_h, box_w, y0, x0)
    win.bkgd(" ", curses.color_pair(C_DEFAULT))
    win.keypad(True)
    yes = default_yes
    while True:
        win.erase()
        draw_box(win, 0, 0, box_h, box_w, title)
        for i, line in enumerate(wrapped):
            addstr_safe(win, 1 + i, 2, line, curses.color_pair(C_DEFAULT))
        yes_attr = curses.color_pair(C_SELECTED) if yes else curses.color_pair(C_DEFAULT)
        no_attr = curses.color_pair(C_SELECTED) if not yes else curses.color_pair(C_DEFAULT)
        bx = box_w - 24
        addstr_safe(win, box_h - 2, bx, "  Yes  ", yes_attr)
        addstr_safe(win, box_h - 2, bx + 10, "  No  ", no_attr)
        win.refresh()
        ch = win.getch()
        if ch in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("\t")):
            yes = not yes
        elif ch in (ord("y"), ord("Y")):
            return True
        elif ch in (ord("n"), ord("N")):
            return False
        elif ch in (10, 13, curses.KEY_ENTER):
            return yes
        elif ch == 27:
            return False


def text_input(stdscr, prompt: str, initial: str = "", masked: bool = False, allow_generate: bool = False, generator: Optional[Callable[[], str]] = None) -> Optional[str]:
    """Modal line editor. Returns the entered string, or None if cancelled."""
    h, w = stdscr.getmaxyx()
    box_w = min(76, w - 4)
    box_h = 7 if allow_generate else 6
    y0 = max(1, (h - box_h) // 2)
    x0 = max(1, (w - box_w) // 2)
    win = curses.newwin(box_h, box_w, y0, x0)
    win.bkgd(" ", curses.color_pair(C_DEFAULT))
    win.keypad(True)

    buf = list(initial)
    cursor = len(buf)
    show = not masked

    while True:
        win.erase()
        draw_box(win, 0, 0, box_h, box_w, "Edit value")
        addstr_safe(win, 1, 2, prompt[: box_w - 4], curses.color_pair(C_HELP))
        # input line
        input_y = 3
        addstr_safe(win, input_y, 2, "> ", curses.color_pair(C_DEFAULT) | curses.A_BOLD)
        display = "".join(buf) if show else "*" * len(buf)
        # scroll horizontally if too long
        max_input_w = box_w - 6
        scroll = max(0, cursor - max_input_w + 1)
        visible = display[scroll: scroll + max_input_w]
        addstr_safe(win, input_y, 4, visible, curses.color_pair(C_DEFAULT))
        # hints
        hint_parts = ["Enter: save", "Esc: cancel"]
        if masked:
            hint_parts.append("Ctrl-T: toggle visibility")
        if allow_generate:
            hint_parts.append("Ctrl-G: generate")
        hint = "  ".join(hint_parts)
        addstr_safe(win, box_h - 2, 2, hint, curses.color_pair(C_DIM))
        # place cursor
        try:
            win.move(input_y, 4 + (cursor - scroll))
        except curses.error:
            pass
        win.refresh()

        ch = win.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return "".join(buf)
        elif ch == 27:  # esc
            return None
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            if cursor > 0:
                del buf[cursor - 1]
                cursor -= 1
        elif ch == curses.KEY_DC:  # delete
            if cursor < len(buf):
                del buf[cursor]
        elif ch == curses.KEY_LEFT:
            cursor = max(0, cursor - 1)
        elif ch == curses.KEY_RIGHT:
            cursor = min(len(buf), cursor + 1)
        elif ch == curses.KEY_HOME:
            cursor = 0
        elif ch == curses.KEY_END:
            cursor = len(buf)
        elif ch == 21:  # ctrl-U: clear
            buf = []
            cursor = 0
        elif ch == 20 and masked:  # ctrl-T: toggle
            show = not show
        elif ch == 7 and allow_generate and generator is not None:  # ctrl-G
            buf = list(generator())
            cursor = len(buf)
            show = True  # reveal newly generated value so user sees what they got
        elif 32 <= ch < 127:
            buf.insert(cursor, chr(ch))
            cursor += 1
        # else ignore


# ============================================================================
# screens
# ============================================================================

def screen_too_small(stdscr) -> None:
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        msg = f"Terminal too small ({w}x{h}). Need at least {MIN_COLS}x{MIN_ROWS}."
        addstr_safe(stdscr, h // 2, max(0, (w - len(msg)) // 2), msg, curses.color_pair(C_ERR))
        msg2 = "Resize the terminal, or press q to quit."
        addstr_safe(stdscr, h // 2 + 1, max(0, (w - len(msg2)) // 2), msg2, curses.color_pair(C_DIM))
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            return
        if curses.is_term_resized(h, w):
            curses.resize_term(*stdscr.getmaxyx())
        h2, w2 = stdscr.getmaxyx()
        if h2 >= MIN_ROWS and w2 >= MIN_COLS:
            return


def screen_profiles(stdscr, state: dict) -> bool:
    """First screen: pick which profiles you'll run. Returns False if cancelled."""
    items = [
        ("core",     "Core stack (always on)",                 "InfluxDB, Postgres, Collector, Balancer, Dashboard", True),
        ("sentinel", "Sentinel — Foresight (recommended)", "Capacity forecasting, right-sizing, REST API",       True),
        ("backup",   "Backup",                                 "Scheduled VM backups/exports to SMB/NFS and imports", True),
    ]
    selected = {key for key, _, _, default in items if default}
    selected.add("core")  # always on, locked

    cursor = 0

    while True:
        stdscr.erase()
        draw_titlebar(stdscr, " HyperCore Manager — Installer  ")
        h, w = stdscr.getmaxyx()
        draw_box(stdscr, 2, 2, h - 4, w - 4, "Step 1 of 2 — Pick the modules you intend to use")

        intro = [
            "Sections of the installer that don't apply to your chosen modules will",
            "be skipped, so you don't have to answer questions you'll never use.",
            "",
            "Toggle with Space.  Core is mandatory.",
        ]
        y = 4
        for line in intro:
            addstr_safe(stdscr, y, 6, line, curses.color_pair(C_DIM))
            y += 1
        y += 1

        for idx, (key, label, blurb, _default) in enumerate(items):
            mark = "[X]" if key in selected else "[ ]"
            if key == "core":
                mark = "[X]"  # locked on
            attr = curses.color_pair(C_SELECTED) if idx == cursor else curses.color_pair(C_DEFAULT)
            line = f" {mark}  {label}"
            addstr_safe(stdscr, y, 6, line.ljust(w - 12), attr)
            addstr_safe(stdscr, y + 1, 12, blurb, curses.color_pair(C_DIM))
            y += 3

        draw_statusbar(stdscr, " ↑↓: navigate   Space: toggle   Enter: continue   Esc: quit ")
        stdscr.refresh()

        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            cursor = (cursor - 1) % len(items)
        elif ch in (curses.KEY_DOWN, ord("j")):
            cursor = (cursor + 1) % len(items)
        elif ch == ord(" "):
            key = items[cursor][0]
            if key == "core":
                continue  # locked
            if key in selected:
                selected.remove(key)
            else:
                selected.add(key)
        elif ch in (10, 13, curses.KEY_ENTER):
            state["profiles"] = selected
            return True
        elif ch in (27, ord("q")):
            return False


def screen_mode(stdscr) -> Optional[str]:
    """Pick easy or advanced mode. Returns 'easy', 'advanced', or None on cancel."""
    items = [
        (
            "easy",
            "Easy mode  (recommended)",
            "Just enter a dashboard password and your time zone; clusters are registered in the dashboard.",
            "Strong secrets are auto-generated; everything else uses sensible defaults.",
        ),
        (
            "advanced",
            "Advanced mode",
            "Walk through every setting in .env with full descriptions and defaults.",
            "Use this for InfluxDB retention, Foresight thresholds, webhooks or paths.",
        ),
    ]
    cursor = 0
    while True:
        stdscr.erase()
        draw_titlebar(stdscr, " HyperCore Manager — Installer  ")
        h, w = stdscr.getmaxyx()
        draw_box(stdscr, 2, 2, h - 4, w - 4, "Pick an installation mode")

        intro = [
            "Most operators want easy mode. Pick advanced if you've read the",
            ".env.template and know which knobs you'd like to turn.",
        ]
        y = 4
        for line in intro:
            addstr_safe(stdscr, y, 6, line, curses.color_pair(C_DIM))
            y += 1
        y += 1

        for idx, (_key, label, blurb, blurb2) in enumerate(items):
            attr = curses.color_pair(C_SELECTED) if idx == cursor else curses.color_pair(C_DEFAULT)
            addstr_safe(stdscr, y, 6, " " * (w - 12), attr)
            marker = " > " if idx == cursor else "   "
            addstr_safe(stdscr, y, 6, marker + label, attr | curses.A_BOLD)
            addstr_safe(stdscr, y + 1, 12, blurb, curses.color_pair(C_DIM))
            addstr_safe(stdscr, y + 2, 12, blurb2, curses.color_pair(C_DIM))
            y += 4

        draw_statusbar(stdscr, " ↑↓: navigate   Enter: continue   Esc: quit ")
        stdscr.refresh()

        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            cursor = (cursor - 1) % len(items)
        elif ch in (curses.KEY_DOWN, ord("j")):
            cursor = (cursor + 1) % len(items)
        elif ch in (10, 13, curses.KEY_ENTER):
            return items[cursor][0]
        elif ch in (27, ord("q"), ord("Q")):
            return None


def screen_easy(stdscr, state: dict) -> bool:
    """Easy-mode: two fields (dashboard password, time zone); every secret is generated.
    Clusters are registered in the dashboard once the stack is running."""
    values: dict[str, str] = state["values"]

    easy_fields = [
        {
            "key": "SC_DASHBOARD_PASSWORD",
            "label": "Dashboard password",
            "hint": "Initial admin password for the web UI on port 5000 — clusters are registered there afterwards",
            "type": "password",
        },
        {
            "key": "TZ",
            "label": "Time zone",
            "hint": "Enter opens a picker: UTC, or a region and then a city. Used for log timestamps and the default of scheduled jobs",
            "type": "timezone",
        },
    ]

    staged: dict[str, str] = {
        "SC_DASHBOARD_PASSWORD": "" if values.get("SC_DASHBOARD_PASSWORD", "") in PLACEHOLDERS else values.get("SC_DASHBOARD_PASSWORD", ""),
        "TZ": values.get("TZ", "") or "UTC",
    }

    cursor = 0
    n_fields = len(easy_fields)
    save_idx = n_fields
    n_items = n_fields + 1

    def commit() -> bool:
        if not staged["SC_DASHBOARD_PASSWORD"]:
            message_box(stdscr, "Missing dashboard password", ["Please enter (or generate) a dashboard admin password."], color_pair=C_WARN)
            return False
        err = validate_timezone(staged["TZ"])
        if err:
            message_box(stdscr, "Invalid time zone", [err], color_pair=C_WARN)
            return False
        values["SC_DASHBOARD_PASSWORD"] = staged["SC_DASHBOARD_PASSWORD"]
        values["TZ"] = staged["TZ"].strip()

        autofill_paths(state["sections"], values)
        apply_easy_autogen(state["sections"], values, state["profiles"])
        return try_save(stdscr, state)

    while True:
        stdscr.erase()
        draw_titlebar(stdscr, " HyperCore Manager — Easy Mode  ")
        h, w = stdscr.getmaxyx()
        draw_box(stdscr, 1, 2, h - 3, w - 4, "Enter the bare minimum — everything else is auto-configured")

        intro = [
            "Strong secrets for InfluxDB, Postgres, dashboard sessions and the",
            "Sentinel, backup and proxy APIs are generated automatically. Repo and",
            "SSH paths are auto-detected. Re-run this installer any time to switch modes.",
        ]
        y = 3
        for line in intro:
            addstr_safe(stdscr, y, 4, line, curses.color_pair(C_DIM))
            y += 1
        y += 1

        label_w = max(len(f["label"]) for f in easy_fields)
        value_x = 8 + label_w + 2

        for idx, f in enumerate(easy_fields):
            v = staged[f["key"]]
            if f["type"] == "password":
                display = "********" if v else ""
            else:
                display = v

            row_attr = curses.color_pair(C_SELECTED) if idx == cursor else curses.color_pair(C_DEFAULT)
            ok = bool(v.strip()) if f["type"] != "password" else bool(v)
            flag = " * " if ok else " ! "
            flag_attr = curses.color_pair(C_OK) if ok else curses.color_pair(C_WARN)

            addstr_safe(stdscr, y, 4, " " * (w - 8), row_attr)
            addstr_safe(stdscr, y, 4, flag, flag_attr | (curses.A_REVERSE if idx == cursor else 0))
            addstr_safe(stdscr, y, 8, f["label"].ljust(label_w), row_attr | curses.A_BOLD)
            value_w = max(1, w - value_x - 6)
            shown = display if display else "(not set)"
            if len(shown) > value_w:
                shown = shown[: value_w - 1] + "…"
            shown_attr = row_attr if display else (row_attr | curses.A_DIM)
            addstr_safe(stdscr, y, value_x, shown, shown_attr)
            addstr_safe(stdscr, y + 1, 8, f["hint"], curses.color_pair(C_DIM))
            y += 2

        y += 1
        all_set = all(
            (staged[f["key"]] if f["type"] == "password" else staged[f["key"]].strip())
            for f in easy_fields
        )
        save_attr = curses.color_pair(C_SELECTED) if cursor == save_idx else curses.color_pair(C_DEFAULT)
        save_label = " W   Generate secrets and write .env" if all_set else " W   (enter the dashboard password and time zone first)"
        addstr_safe(stdscr, y, 4, " " * (w - 8), save_attr)
        addstr_safe(stdscr, y, 4, save_label, save_attr | curses.A_BOLD)

        hints = " ↑↓: navigate   Enter: edit   "
        cur_field = easy_fields[cursor] if cursor < n_fields else None
        if cur_field and cur_field["key"] == "SC_DASHBOARD_PASSWORD":
            hints += "G: generate   "
        hints += "W: write   Esc: quit "
        draw_statusbar(stdscr, hints)
        stdscr.refresh()

        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            cursor = (cursor - 1) % n_items
        elif ch in (curses.KEY_DOWN, ord("j")):
            cursor = (cursor + 1) % n_items
        elif ch in (10, 13, curses.KEY_ENTER):
            if cur_field is not None and cur_field["type"] == "timezone":
                new = pick_timezone(stdscr, staged[cur_field["key"]])
                if new is not None:
                    staged[cur_field["key"]] = new
            elif cur_field is not None:
                masked = (cur_field["type"] == "password")
                allow_gen = (cur_field["key"] == "SC_DASHBOARD_PASSWORD")
                generator = (lambda: gen_password(20)) if allow_gen else None
                prompt = cur_field["label"]
                if masked:
                    prompt += "    Ctrl-T to toggle visibility"
                new = text_input(
                    stdscr,
                    prompt=prompt,
                    initial=staged[cur_field["key"]],
                    masked=masked,
                    allow_generate=allow_gen,
                    generator=generator,
                )
                if new is not None:
                    err = validate_timezone(new) if cur_field["type"] == "timezone" else None
                    if err:
                        message_box(stdscr, "Invalid time zone", [err, "", f"You entered: {new}"], color_pair=C_ERR)
                    else:
                        staged[cur_field["key"]] = new.strip() if cur_field["type"] == "timezone" else new
            else:
                if commit():
                    return True
        elif ch in (ord("g"), ord("G")) and cur_field and cur_field["key"] == "SC_DASHBOARD_PASSWORD":
            new = gen_password(20)
            if confirm_box(
                stdscr,
                "Generated password",
                [f"Generated dashboard password:", "", new, "", "Use this value?"],
                default_yes=True,
            ):
                staged["SC_DASHBOARD_PASSWORD"] = new
        elif ch in (ord("w"), ord("W")):
            if commit():
                return True
        elif ch in (27, ord("q"), ord("Q")):
            if confirm_box(stdscr, "Quit", ["Discard input and quit?"], default_yes=False):
                return False


def screen_main_menu(stdscr, state: dict) -> str:
    """Main menu. Returns 'save', 'quit', or selects a section then loops."""
    sections: list[Section] = state["sections"]
    values: dict[str, str] = state["values"]
    profiles: set[str] = state["profiles"]

    # build list of visible sections + actions
    visible = [s for s in sections if s.profiles & profiles]
    cursor = 0

    autofill_paths(visible, values)

    while True:
        stdscr.erase()
        draw_titlebar(stdscr, " HyperCore Manager — Installer  ")
        h, w = stdscr.getmaxyx()

        left_w = max(40, w * 2 // 3)
        right_w = w - left_w - 4

        draw_box(stdscr, 2, 2, h - 4, left_w, "Step 2 of 2 — Configure each section")

        y = 4
        addstr_safe(stdscr, y, 6, "Sections", curses.color_pair(C_HELP) | curses.A_BOLD)
        y += 2

        # status legend at bottom of left pane
        for idx, section in enumerate(visible):
            status = section_status(section, values, profiles)
            if status == "ok":
                mark, mark_attr = " * ", curses.color_pair(C_OK)
            elif status == "warn":
                mark, mark_attr = " ! ", curses.color_pair(C_WARN)
            else:
                mark, mark_attr = " - ", curses.color_pair(C_DIM)
            row_attr = curses.color_pair(C_SELECTED) if idx == cursor else curses.color_pair(C_DEFAULT)
            addstr_safe(stdscr, y, 6, " " * (left_w - 8), row_attr)
            addstr_safe(stdscr, y, 6, mark, mark_attr | (curses.A_REVERSE if idx == cursor else 0))
            addstr_safe(stdscr, y, 10, section.title, row_attr | curses.A_BOLD)
            y += 1

        # action rows
        y += 1
        action_index = len(visible)
        action_attr = curses.color_pair(C_SELECTED) if cursor == action_index else curses.color_pair(C_DEFAULT)
        addstr_safe(stdscr, y, 6, " " * (left_w - 8), action_attr)
        addstr_safe(stdscr, y, 6, " W   Write .env and exit", action_attr | curses.A_BOLD)
        y += 1
        action_index_quit = len(visible) + 1
        quit_attr = curses.color_pair(C_SELECTED) if cursor == action_index_quit else curses.color_pair(C_DEFAULT)
        addstr_safe(stdscr, y, 6, " " * (left_w - 8), quit_attr)
        addstr_safe(stdscr, y, 6, " Q   Quit without saving", quit_attr | curses.A_BOLD)

        # right pane: section description for whichever item is highlighted
        draw_box(stdscr, 2, left_w + 4, h - 4, right_w, "About this section")
        if cursor < len(visible):
            section = visible[cursor]
            ry = 4
            addstr_safe(stdscr, ry, left_w + 6, section.title, curses.color_pair(C_HELP) | curses.A_BOLD)
            ry += 2
            for line in wrap_text(section.blurb, right_w - 4):
                addstr_safe(stdscr, ry, left_w + 6, line, curses.color_pair(C_DEFAULT))
                ry += 1
            ry += 1
            n_fields = sum(1 for f in section.fields if f.profiles & profiles)
            addstr_safe(stdscr, ry, left_w + 6, f"{n_fields} field(s)", curses.color_pair(C_DIM))
            ry += 1
            status = section_status(section, values, profiles)
            if status == "warn":
                addstr_safe(stdscr, ry, left_w + 6, "Status: needs attention (placeholders remain)", curses.color_pair(C_WARN))
            elif status == "ok":
                addstr_safe(stdscr, ry, left_w + 6, "Status: looks good", curses.color_pair(C_OK))
            else:
                addstr_safe(stdscr, ry, left_w + 6, "Status: skipped", curses.color_pair(C_DIM))

        # bottom legend
        legend_y = h - 5
        addstr_safe(stdscr, legend_y, left_w + 6, "Legend:", curses.color_pair(C_DIM))
        addstr_safe(stdscr, legend_y + 1, left_w + 6, " *  ready", curses.color_pair(C_OK))
        addstr_safe(stdscr, legend_y + 2, left_w + 6, " !  needs attention", curses.color_pair(C_WARN))
        addstr_safe(stdscr, legend_y + 3, left_w + 6, " -  skipped", curses.color_pair(C_DIM))

        draw_statusbar(stdscr, " ↑↓: navigate   Enter: open section   W: write .env   Q: quit ")
        stdscr.refresh()

        n_items = len(visible) + 2
        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            cursor = (cursor - 1) % n_items
        elif ch in (curses.KEY_DOWN, ord("j")):
            cursor = (cursor + 1) % n_items
        elif ch in (10, 13, curses.KEY_ENTER):
            if cursor < len(visible):
                screen_section_form(stdscr, state, visible[cursor])
            elif cursor == action_index:
                if try_save(stdscr, state):
                    return "save"
            else:
                if confirm_box(stdscr, "Quit", ["Discard changes and quit?"], default_yes=False):
                    return "quit"
        elif ch in (ord("w"), ord("W")):
            if try_save(stdscr, state):
                return "save"
        elif ch in (ord("q"), ord("Q"), 27):
            if confirm_box(stdscr, "Quit", ["Discard changes and quit?"], default_yes=False):
                return "quit"


def try_save(stdscr, state: dict) -> bool:
    """Validate and write .env. Returns True on success."""
    sections: list[Section] = state["sections"]
    values: dict[str, str] = state["values"]
    profiles: set[str] = state["profiles"]

    bad = collect_validation_errors(sections, values, profiles)

    # in easy mode the SSH key path is informational — updates pull from a
    # public git remote — so suppress that one specific warning
    skip_path_keys = {"SC_SSH_KEY_PATH"} if state.get("mode") == "easy" else set()
    path_warnings = collect_path_warnings(sections, values, profiles, skip_keys=skip_path_keys)

    if bad:
        message_box(
            stdscr,
            "Cannot save — fields need attention",
            ["The following fields still hold placeholder values or are invalid:", ""] + bad +
            ["", "Open the highlighted sections (marked '!') and resolve them."],
            color_pair=C_WARN,
        )
        return False

    if path_warnings:
        proceed = confirm_box(
            stdscr,
            "Path warning",
            ["The following auto-detected paths do not currently exist:", ""] + path_warnings +
            ["", "These are mounted INTO containers. If they don't exist on the",
             "host the dashboard auto-update + SSH features will not work.",
             "", "Save anyway?"],
            default_yes=False,
        )
        if not proceed:
            return False

    backup = write_env(ENV_PATH, sections, values, profiles)
    msg = [f"Wrote {ENV_PATH.resolve()} (mode 0600 — owner-only).", ""]
    if backup:
        msg.append(f"Previous .env backed up to {backup.name}.")
        msg.append("")
    msg.extend([
        "Next steps:",
        "  docker compose build",
        "  docker compose up -d",
    ])
    msg.append("  (every module starts with the stack; stop the ones you don't use, e.g. docker compose stop sentinel)")
    msg.extend(["", "Every cluster starts in dry-run. Switch it off per cluster in the dashboard",
                "(Clusterbalancer -> settings) once its proposed migrations look sensible."])

    status, version = detect_compose()
    if status == 'v2':
        msg.extend(["", f"Detected: Compose v{version}."])
        color = C_OK
    elif status == 'v1_only':
        msg = [
            "WARNING: Detected docker-compose v1 (python).",
            f"  Found: {version}",
            "  This version has a known recreate bug",
            "  (KeyError: 'ContainerConfig') and is unsupported.",
            "  Upgrade before running the next steps:",
            "    sudo apt remove docker-compose",
            "    sudo apt install docker-compose-v2",
            "",
        ] + msg
        color = C_WARN
    else:
        msg = [
            "ERROR: Neither 'docker compose' nor 'docker-compose'",
            "       was found on PATH.",
            "  Install Compose v2 before running the next steps:",
            "    sudo apt install docker-compose-v2",
            "",
        ] + msg
        color = C_ERR

    message_box(stdscr, "Saved", msg, color_pair=color)
    return True


def screen_section_form(stdscr, state: dict, section: Section) -> None:
    """Field-edit form for a single section."""
    values: dict[str, str] = state["values"]
    profiles: set[str] = state["profiles"]

    fields = [f for f in section.fields if f.profiles & profiles]
    cursor = 0

    while True:
        stdscr.erase()
        draw_titlebar(stdscr, f" HyperCore Manager — {section.title}  ")
        h, w = stdscr.getmaxyx()

        # layout: top half = fields, bottom half = description of focused field
        top_h = max(8, h // 2)
        bot_h = h - top_h - 2  # minus title + status bars

        draw_box(stdscr, 1, 2, top_h, w - 4, section.title)

        # show enabled-state notice if relevant
        info_line: Optional[str] = None
        if section.enable_field:
            ev = values.get(section.enable_field, "")
            ef = next((f for f in section.fields if f.key == section.enable_field), None)
            if ef:
                if ef.type == "bool" and ev.lower() == "false":
                    info_line = f"NOTE: {section.enable_field}=false — other fields stored but unused"
                elif ef.type != "bool" and not ev:
                    info_line = f"NOTE: {section.enable_field} empty — other fields stored but unused"

        y = 3
        if info_line:
            addstr_safe(stdscr, y, 4, info_line, curses.color_pair(C_DIM) | curses.A_ITALIC if hasattr(curses, "A_ITALIC") else curses.color_pair(C_DIM))
            y += 2

        # compute label column width
        label_w = max((len(f.label) for f in fields), default=10)
        label_w = min(label_w, 28)

        for idx, f in enumerate(fields):
            v = values.get(f.key, f.default)
            display_val = "********" if f.type == "password" and v else v
            if f.type == "token" and v and v not in PLACEHOLDERS:
                display_val = (v[:8] + "..." + v[-4:]) if len(v) > 16 else v

            row_attr = curses.color_pair(C_SELECTED) if idx == cursor else curses.color_pair(C_DEFAULT)
            # status flag
            placeholder = f.must_change and v in PLACEHOLDERS
            err = validate_value(f, v)
            if placeholder:
                flag, flag_attr = " ! ", curses.color_pair(C_WARN)
            elif err:
                flag, flag_attr = " ! ", curses.color_pair(C_ERR)
            elif v != f.default and v != "":
                flag, flag_attr = " * ", curses.color_pair(C_OK)
            else:
                flag, flag_attr = "   ", curses.color_pair(C_DIM)

            addstr_safe(stdscr, y, 4, " " * (w - 8), row_attr)
            addstr_safe(stdscr, y, 4, flag, flag_attr | (curses.A_REVERSE if idx == cursor else 0))
            addstr_safe(stdscr, y, 8, f.label.ljust(label_w), row_attr | curses.A_BOLD)
            value_x = 8 + label_w + 2
            value_w = max(1, w - value_x - 6)
            shown = display_val if len(display_val) <= value_w else display_val[: value_w - 1] + "…"
            addstr_safe(stdscr, y, value_x, shown, row_attr)
            y += 1
            if y >= 1 + top_h - 1:
                break

        # bottom: description of focused field
        bot_y = top_h + 2
        cur_field = fields[cursor] if fields else None
        if cur_field:
            draw_box(stdscr, bot_y - 1, 2, bot_h, w - 4, f"About: {cur_field.key}")
            content_w = w - 8
            ly = bot_y
            for line in wrap_text(cur_field.description, content_w):
                addstr_safe(stdscr, ly, 4, line, curses.color_pair(C_DEFAULT))
                ly += 1
            ly += 1
            addstr_safe(stdscr, ly, 4, "Default: ", curses.color_pair(C_HELP) | curses.A_BOLD)
            addstr_safe(stdscr, ly, 13, cur_field.default or "(empty)", curses.color_pair(C_DEFAULT))
            ly += 1
            for line in wrap_text(cur_field.why_default, content_w - 4):
                addstr_safe(stdscr, ly, 8, line, curses.color_pair(C_DIM))
                ly += 1
            ly += 1
            addstr_safe(stdscr, ly, 4, "When change: ", curses.color_pair(C_HELP) | curses.A_BOLD)
            ly += 1
            for line in wrap_text(cur_field.when_change, content_w - 4):
                addstr_safe(stdscr, ly, 8, line, curses.color_pair(C_DEFAULT))
                ly += 1
            # show autofill notice if relevant
            if cur_field.autofill:
                detected = cur_field.autofill()
                ly += 1
                addstr_safe(stdscr, ly, 4, "Auto-detected: ", curses.color_pair(C_HELP) | curses.A_BOLD)
                addstr_safe(stdscr, ly, 19, detected, curses.color_pair(C_DEFAULT))
                if not path_exists(detected):
                    ly += 1
                    addstr_safe(stdscr, ly, 4, "WARNING: detected path does not exist on this host.", curses.color_pair(C_ERR))

        hints = " ↑↓: navigate   Enter: edit   "
        if cur_field and cur_field.type == "bool":
            hints += "Space: toggle   "
        if cur_field and cur_field.type in ("password", "token"):
            hints += "G: generate   "
        if cur_field and cur_field.autofill:
            hints += "A: autofill   "
        hints += "R: reset to default   Esc: back to menu "
        draw_statusbar(stdscr, hints)
        stdscr.refresh()

        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            cursor = (cursor - 1) % len(fields)
        elif ch in (curses.KEY_DOWN, ord("j")):
            cursor = (cursor + 1) % len(fields)
        elif ch in (10, 13, curses.KEY_ENTER):
            edit_field(stdscr, state, cur_field) if cur_field else None
        elif ch == ord(" ") and cur_field and cur_field.type == "bool":
            cur = values.get(cur_field.key, cur_field.default)
            values[cur_field.key] = "false" if cur.lower() == "true" else "true"
        elif ch in (ord("g"), ord("G")) and cur_field and cur_field.type in ("password", "token"):
            generate_for_field(stdscr, state, cur_field)
        elif ch in (ord("a"), ord("A")) and cur_field and cur_field.autofill:
            values[cur_field.key] = cur_field.autofill()
        elif ch in (ord("r"), ord("R")) and cur_field:
            if confirm_box(stdscr, "Reset", [f"Reset {cur_field.key} to its default value?", "", f"Default: {cur_field.default or '(empty)'}"]):
                values[cur_field.key] = cur_field.default
        elif ch in (27, ord("q")):
            return


def generate_for_field(stdscr, state: dict, f: Field) -> None:
    values: dict[str, str] = state["values"]
    if f.type == "token":
        new = gen_token(32)
    else:
        new = gen_password(24)
    if not confirm_box(
        stdscr,
        f"Generated {f.type}",
        [f"Generated value for {f.key}:", "", new, "", "Use this value?"],
        default_yes=True,
    ):
        return
    values[f.key] = new


def edit_field(stdscr, state: dict, f: Field) -> None:
    values: dict[str, str] = state["values"]
    current = values.get(f.key, f.default)

    if f.type == "bool":
        new = "false" if current.lower() == "true" else "true"
        values[f.key] = new
        return

    if f.type == "choice" and f.choices:
        new = pick_choice(stdscr, f, current)
        if new is not None:
            values[f.key] = new
        return

    if f.type == "timezone":
        new = pick_timezone(stdscr, current)
        if new is not None:
            values[f.key] = new
        return

    masked = (f.type == "password")
    allow_gen = (f.type in ("password", "token"))
    generator = (lambda: gen_password(24)) if f.type == "password" else (lambda: gen_token(32))

    prompt = f"{f.key}  ({f.type})"
    if f.type == "password":
        prompt += "    Ctrl-T to toggle visibility"

    new = text_input(
        stdscr,
        prompt=prompt,
        initial=current,
        masked=masked,
        allow_generate=allow_gen,
        generator=generator,
    )
    if new is None:
        return

    err = validate_value(f, new)
    if err:
        message_box(stdscr, "Invalid value", [err, "", f"You entered: {new}"], color_pair=C_ERR)
        return
    values[f.key] = new


def pick_choice(stdscr, f: Field, current: str) -> Optional[str]:
    if not f.choices:
        return None
    h, w = stdscr.getmaxyx()
    box_w = min(50, w - 4)
    box_h = len(f.choices) + 4
    y0 = max(1, (h - box_h) // 2)
    x0 = max(1, (w - box_w) // 2)
    win = curses.newwin(box_h, box_w, y0, x0)
    win.bkgd(" ", curses.color_pair(C_DEFAULT))
    win.keypad(True)
    try:
        cursor = f.choices.index(current)
    except ValueError:
        cursor = 0
    while True:
        win.erase()
        draw_box(win, 0, 0, box_h, box_w, f.label)
        for i, c in enumerate(f.choices):
            attr = curses.color_pair(C_SELECTED) if i == cursor else curses.color_pair(C_DEFAULT)
            addstr_safe(win, 1 + i, 2, " " * (box_w - 4), attr)
            addstr_safe(win, 1 + i, 4, c, attr)
        win.refresh()
        ch = win.getch()
        if ch in (curses.KEY_UP, ord("k")):
            cursor = (cursor - 1) % len(f.choices)
        elif ch in (curses.KEY_DOWN, ord("j")):
            cursor = (cursor + 1) % len(f.choices)
        elif ch in (10, 13, curses.KEY_ENTER):
            return f.choices[cursor]
        elif ch == 27:
            return None


def pick_from_list(stdscr, title: str, items: list[str], current: str = "", hint: str = "") -> Optional[str]:
    """Scrolling list box. Typing narrows the list (case-insensitive substring), Backspace widens
    it, Enter picks, Esc cancels. Returns the chosen item or None."""
    if not items:
        return None
    h, w = stdscr.getmaxyx()
    box_w = min(max(44, max(len(x) for x in items) + 10), w - 4)
    box_h = min(len(items) + 6, h - 4)
    rows = box_h - 6
    y0 = max(1, (h - box_h) // 2)
    x0 = max(1, (w - box_w) // 2)
    win = curses.newwin(box_h, box_w, y0, x0)
    win.bkgd(" ", curses.color_pair(C_DEFAULT))
    win.keypad(True)
    query = ""
    cursor = items.index(current) if current in items else 0
    top = 0
    while True:
        shown = [x for x in items if query.lower() in x.lower()] if query else items
        if not shown:
            cursor = 0
        else:
            cursor = max(0, min(cursor, len(shown) - 1))
        if cursor < top:
            top = cursor
        if cursor >= top + rows:
            top = cursor - rows + 1
        win.erase()
        draw_box(win, 0, 0, box_h, box_w, title)
        addstr_safe(win, 1, 2, (f"Filter: {query}" if query else (hint or "Type to filter")).ljust(box_w - 4), curses.color_pair(C_DIM))
        for i, item in enumerate(shown[top:top + rows]):
            idx = top + i
            attr = curses.color_pair(C_SELECTED) if idx == cursor else curses.color_pair(C_DEFAULT)
            addstr_safe(win, 3 + i, 2, " " * (box_w - 4), attr)
            addstr_safe(win, 3 + i, 4, item[: box_w - 8], attr)
        if not shown:
            addstr_safe(win, 3, 4, "(nothing matches)", curses.color_pair(C_DIM))
        more = f" {cursor + 1}/{len(shown)} " if shown else ""
        addstr_safe(win, box_h - 2, 2, (" ↑↓ move   Enter pick   Esc back   " + more)[: box_w - 4], curses.color_pair(C_DIM))
        win.refresh()
        ch = win.getch()
        if ch == curses.KEY_UP:
            cursor -= 1
        elif ch == curses.KEY_DOWN:
            cursor += 1
        elif ch == curses.KEY_PPAGE:
            cursor -= rows
        elif ch == curses.KEY_NPAGE:
            cursor += rows
        elif ch in (10, 13, curses.KEY_ENTER):
            if shown:
                return shown[cursor]
        elif ch == 27:
            return None
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            query = query[:-1]
        elif 32 <= ch < 127:
            query += chr(ch)
            cursor = 0


def pick_timezone(stdscr, current: str = "") -> Optional[str]:
    """Two stages from the host's tz database: a region (or UTC), then its city. Esc in the
    city list goes back to the regions; Esc there cancels. Returns the IANA name or None."""
    cur_region = current.split("/", 1)[0] if "/" in (current or "") else (current or "")
    while True:
        region = pick_from_list(stdscr, "Time zone — region", timezone_regions(), cur_region, "UTC, or a region to pick a city from")
        if region is None:
            return None
        if region == "UTC":
            return "UTC"
        cities = timezone_cities(region)
        if not cities:
            message_box(stdscr, "No zones", [f"The host's time zone database has no zones under {region}/."], color_pair=C_WARN)
            continue
        cur_city = current[len(region) + 1:] if (current or "").startswith(region + "/") else ""
        city = pick_from_list(stdscr, f"Time zone — {region}/…", cities, cur_city, "Type a city name to filter")
        if city is None:
            cur_region = region
            continue
        return f"{region}/{city}"


# ============================================================================
# entry point
# ============================================================================

def run(stdscr) -> None:
    init_colors()
    curses.curs_set(0)
    stdscr.keypad(True)

    h, w = stdscr.getmaxyx()
    if h < MIN_ROWS or w < MIN_COLS:
        screen_too_small(stdscr)
        h, w = stdscr.getmaxyx()
        if h < MIN_ROWS or w < MIN_COLS:
            return

    sections = build_schema()
    existing = parse_env(ENV_PATH)

    # seed values: start from defaults, then overlay any existing .env values
    values = seed_default_values(sections)
    values.update(existing)

    state = {
        "sections": sections,
        "values": values,
        "profiles": {"core"},
        "existing": bool(existing),
        "mode": "advanced",
    }

    if existing:
        start_from_existing = confirm_box(
            stdscr,
            "Existing .env detected",
            [
                f"Found existing {ENV_PATH.resolve()}.",
                "The current file is backed up to .env.bak.<timestamp> when you write.",
                "",
                "Start from its values?",
                "",
                "  Yes: upgrading or reconfiguring a running installation. Every value,",
                "       secrets included, is pre-filled. The database keeps working:",
                "       HCM_SECRET_KEY encrypts the stored cluster passwords, and the",
                "       PostgreSQL and InfluxDB passwords only apply to new volumes.",
                "  No:  fresh install after 'docker compose down -v'. The file is ignored",
                "       and everything starts blank, as if it were not there.",
            ],
            default_yes=True,
        )
        if not start_from_existing:
            values = seed_default_values(sections)
            state["values"] = values
            state["existing"] = False

    mode = screen_mode(stdscr)
    if mode is None:
        return
    state["mode"] = mode

    if mode == "easy":
        # easy mode runs core + sentinel (the recommended baseline) and skips
        # the per-section configuration screen entirely
        state["profiles"] = set(EASY_PROFILES)
        screen_easy(stdscr, state)
        return

    if not screen_profiles(stdscr, state):
        return

    while True:
        result = screen_main_menu(stdscr, state)
        if result in ("save", "quit"):
            return


def main() -> int:
    if not TEMPLATE_PATH.exists():
        print(f"Could not find {TEMPLATE_PATH} in the current directory.", file=sys.stderr)
        print("Run this script from the hypercore-manager repo root.", file=sys.stderr)
        return 1
    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
