"""Curses front-end: rendering, input editing, commands, presence, alerts."""

import curses
import re
import sys
import textwrap
import time

from . import __version__
from .colors import UserColorRegistry, color_state, init_user_colors, normalize_hex_color
from .store import ChatStore

POLL_SECONDS = 0.15
HEARTBEAT_SECONDS = 5.0
TYPING_REFRESH_SECONDS = 2.0
MAX_MESSAGE_LEN = 2000
MAX_HISTORY_ITEMS = 100
MAX_RENDERED_RECORDS = 5000
REACTION_DEFAULT = "\U0001F44D"

NICK_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"

HELP_LINES = (
    "commands:",
    "  /color #rrggbb    set your message color",
    "  /me action        send an action message",
    "  /nick name        change your display name",
    "  /rooms            list rooms on this machine",
    "  /room name        switch to another room",
    "  /reply N text     reply to message number N",
    "  /react N [emoji]  react to message number N",
    "  /clear            clear room history for everyone",
    "  /quit             exit",
    "keys: Up/Down input history | Ctrl-A/E line start/end | Ctrl-W word delete",
    "      Ctrl-U/K clear before/after cursor | Left/Right move | PgUp/PgDn scroll",
)


def visible_len(value):
    return len(value.replace("\t", "    "))


def add_line(win, y, x, text, width, attr=0):
    if y < 0 or y >= win.getmaxyx()[0] or width <= 0:
        return
    try:
        win.addnstr(y, x, text, width, attr)
    except curses.error:
        pass


def format_stamp(ts):
    local = time.localtime(float(ts))
    if time.strftime("%Y-%m-%d", local) == time.strftime("%Y-%m-%d"):
        return time.strftime("%H:%M", local)
    return time.strftime("%m-%d %H:%M", local)


def mention_pattern(nick):
    return re.compile(r"(?<![\w.-])" + re.escape(nick) + r"(?![\w.-])", re.IGNORECASE)


def fold_reactions(records):
    """Fold reaction records in order; a repeat by the same user toggles off."""
    folded = {}
    for record in records:
        if record.get("type") != "reaction":
            continue
        target = record["target"]
        emoji = record["emoji"]
        users = folded.setdefault(target, {}).setdefault(emoji, [])
        user = str(record.get("user", ""))
        if user in users:
            users.remove(user)
        else:
            users.append(user)
    return {
        target: {emoji: users for emoji, users in per_target.items() if users}
        for target, per_target in folded.items()
    }


class InputLine:
    """Single-line editor with cursor movement, kill commands and history."""

    def __init__(self):
        self.text = ""
        self.cursor = 0
        self.history = []
        self.hpos = 0
        self.draft = ""

    def insert(self, chunk):
        if not chunk:
            return
        pos = self.cursor
        self.text = self.text[:pos] + chunk + self.text[pos:]
        self.cursor = pos + len(chunk)

    def backspace(self):
        if self.cursor > 0:
            self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
            self.cursor -= 1

    def delete(self):
        if self.cursor < len(self.text):
            self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]

    def left(self):
        self.cursor = max(0, self.cursor - 1)

    def right(self):
        self.cursor = min(len(self.text), self.cursor + 1)

    def home(self):
        self.cursor = 0

    def end(self):
        self.cursor = len(self.text)

    def kill_before(self):
        self.text = self.text[self.cursor :]
        self.cursor = 0

    def kill_after(self):
        self.text = self.text[: self.cursor]

    def kill_word(self):
        match = re.search(r"\S*\s*$", self.text[: self.cursor])
        start = match.start() if match else 0
        self.text = self.text[:start] + self.text[self.cursor :]
        self.cursor = start

    def load_history(self, offset):
        self.text = self.history[offset]
        self.cursor = len(self.text)

    def hist_up(self):
        if not self.history:
            return
        if self.hpos == len(self.history):
            self.draft = self.text
        self.hpos = max(0, self.hpos - 1)
        self.load_history(self.hpos)

    def hist_down(self):
        if self.hpos >= len(self.history):
            return
        self.hpos += 1
        if self.hpos == len(self.history):
            self.text = self.draft
            self.cursor = len(self.text)
        else:
            self.load_history(self.hpos)

    def submit(self):
        text = self.text.strip()
        if text:
            self.history.append(text)
            del self.history[:-MAX_HISTORY_ITEMS]
            self.hpos = len(self.history)
            self.draft = ""
        self.text = ""
        self.cursor = 0
        return text

    def view(self, width):
        """Visible slice of the input keeping the cursor on screen."""
        width = max(1, width)
        if self.cursor < width:
            start = 0
        else:
            start = self.cursor - width + 1
        return self.text[start : start + width], start


class Renderer:
    """Turns records + notes into display lines of (text, pair_id, flags).

    Headless-safe: never calls curses.color_pair; the draw loop maps pair ids.
    """

    def __init__(self, get_nick):
        self.get_nick = get_nick
        self.registry = UserColorRegistry()

    def render(self, records, notes, width):
        body_width = max(10, width - 2)
        defaults, overrides = color_state(records)
        reactions = fold_reactions(records)
        by_id = {r["id"]: r for r in records if r.get("id")}
        ambiguous = self._ambiguous_nicks(records)
        chat = [r for r in records if r.get("type") in ("message", "action")]
        feed = sorted(chat + list(notes), key=lambda item: item["ts"])

        lines = []
        number_map = {}
        number = 0
        prev_day = None
        for item in feed:
            if item.get("type") == "note":
                text = f"   · {item['text']}"[:body_width]
                lines.append((text, None, curses.A_DIM))
                continue
            number += 1
            number_map[number] = item["id"]
            day = time.strftime("%Y-%m-%d", time.localtime(item["ts"]))
            if day != prev_day:
                prev_day = day
                label = time.strftime("%A, %b %d %Y", time.localtime(item["ts"]))
                lines.append(self._separator(label, body_width))
            lines.extend(
                self._render_message(item, body_width, defaults, overrides, by_id, ambiguous)
            )
            per_emoji = reactions.get(item["id"])
            if per_emoji:
                lines.append(self._reaction_line(per_emoji, body_width))
        return lines, number_map

    @staticmethod
    def _ambiguous_nicks(records):
        uids_by_nick = {}
        for record in records:
            uid = record.get("uid")
            if uid is None:
                continue
            uids_by_nick.setdefault(str(record.get("user", "")), set()).add(uid)
        return {nick for nick, uids in uids_by_nick.items() if len(uids) > 1}

    def _display(self, record, ambiguous):
        user = str(record.get("user", ""))
        if user in ambiguous and record.get("uid") is not None:
            return f"{user}·{record['uid']}"
        return user

    @staticmethod
    def _separator(label, body_width):
        side = max(2, (body_width - len(label) - 4) // 2)
        text = f" {'─' * side} {label} {'─' * side}"
        return (text[:body_width], None, curses.A_DIM)

    def _render_message(self, item, body_width, defaults, overrides, by_id, ambiguous):
        user = str(item["user"])
        display = self._display(item, ambiguous)
        pair = self.registry.pair_for(defaults.get(user, 10), overrides.get(user))
        mentioned = False
        if item["type"] == "message" and user != self.get_nick():
            mentioned = mention_pattern(self.get_nick()).search(item["text"]) is not None
        flags = curses.A_UNDERLINE if mentioned else 0

        prefix_lines = []
        reply_to = item.get("reply_to")
        target = by_id.get(reply_to) if reply_to else None
        if target is not None:
            t_display = self._display(target, ambiguous)
            if target.get("type") == "action":
                t_text = f"{t_display} {target.get('text', '')}"
            else:
                t_text = f"{t_display}: {target.get('text', '')}"
            quote = f"   > {t_text}"[:body_width]
            prefix_lines.append((quote, None, curses.A_DIM))

        stamp = format_stamp(item["ts"])
        if item["type"] == "action":
            prefix = f"{stamp} * "
            text = f"{display} {item['text']}"
        else:
            prefix = f"{stamp} {display}: "
            text = str(item["text"]).replace("\n", " ")

        first_width = max(8, body_width - visible_len(prefix))
        wrapped = textwrap.wrap(text, width=first_width, replace_whitespace=False) or [""]
        prefix_lines.append((prefix + wrapped[0], pair, flags | curses.A_BOLD))
        cont_prefix = " " * min(visible_len(prefix), body_width - 1)
        cont_width = max(8, body_width - visible_len(cont_prefix))
        for part in wrapped[1:]:
            for continued in textwrap.wrap(part, width=cont_width, replace_whitespace=False) or [""]:
                prefix_lines.append((cont_prefix + continued, pair, flags))
        return prefix_lines

    @staticmethod
    def _reaction_line(per_emoji, body_width):
        parts = []
        for emoji, users in per_emoji.items():
            names = "/".join(users)
            parts.append(f"{emoji} {names}" if len(names) <= 24 else f"{emoji} ×{len(users)}")
        text = "   " + "  ".join(parts)
        return (text[:body_width], None, 0)


class App:
    def __init__(self, store, nick):
        self.store = store
        self.nick = nick
        self.stdscr = None
        self.renderer = None
        self.input = InputLine()
        self.messages = []
        self.notes = []
        self.number_map = {}
        self.online_names = []
        self.typing_names = []
        self.known_online = None
        self.follow = True
        self.scroll = 0
        self.status = ""
        self.status_until = 0.0
        self.total_lines = 0
        self.body_height = 1
        self.flash_pending = False
        self.beep_pending = False
        self.last_beat = 0.0
        self.last_type_ping = 0.0
        self._typing_on = False
        self.mention_rx = mention_pattern(nick)

    # ------------------------------------------------------------------- run

    def run(self, stdscr):
        self.stdscr = stdscr
        curses.curs_set(1)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        init_user_colors()
        self.renderer = Renderer(lambda: self.nick)
        stdscr.timeout(int(POLL_SECONDS * 1000))
        self._paste_mode(True)
        try:
            while True:
                self.poll_updates()
                self.draw_frame()
                key = self.read_key()
                if time.time() >= self.status_until:
                    self.status = ""
                if key is None:
                    continue
                if self.handle_key(key) is False:
                    break
        finally:
            self._paste_mode(False)
            self.store.set_typing(self.nick, False)

    @staticmethod
    def _paste_mode(enable):
        try:
            sys.stdout.write("\x1b[?2004h" if enable else "\x1b[?2004l")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

    # ----------------------------------------------------------------- input

    def read_key(self):
        try:
            key = self.stdscr.get_wch()
        except curses.error:
            return None
        if isinstance(key, int):
            return key
        if key != "\x1b":
            return key
        seq = self._drain_chars(len(PASTE_START))
        if not seq.startswith(PASTE_START):
            return None  # lone escape or unhandled sequence: swallow
        buffer = seq[len(PASTE_START) :]
        deadline = time.time() + 2.0
        while not buffer.endswith(PASTE_END):
            if time.time() > deadline or len(buffer) > 200_000:
                break
            try:
                char = self.stdscr.get_wch()
            except curses.error:
                time.sleep(0.01)
                continue
            if isinstance(char, int):
                break
            buffer += char
        buffer = buffer.removesuffix(PASTE_END)
        cleaned = "".join(max(ch, " ") for ch in buffer).replace("\n", " ")
        return ("paste", cleaned)

    def _drain_chars(self, count):
        collected = []
        self.stdscr.timeout(0)
        try:
            while len(collected) < count:
                try:
                    char = self.stdscr.get_wch()
                except curses.error:
                    break
                if isinstance(char, int):
                    break
                collected.append(char)
        finally:
            self.stdscr.timeout(int(POLL_SECONDS * 1000))
        return "".join(collected)

    def handle_key(self, key):
        inp = self.input
        if isinstance(key, tuple) and key[0] == "paste":
            inp.insert(key[1])
            self.ping_typing(force=True)
            return True
        if isinstance(key, int):
            if key in (curses.KEY_RESIZE, 12):  # Ctrl-L redraws
                return True
            if key == curses.KEY_PPAGE:
                self.follow = False
                self.scroll = min(
                    self.scroll + max(1, self.body_height // 2),
                    max(0, self.total_lines - self.body_height),
                )
            elif key == curses.KEY_NPAGE:
                self.scroll = max(0, self.scroll - max(1, self.body_height // 2))
                self.follow = self.scroll == 0
            elif key == curses.KEY_HOME:
                self.follow = False
                self.scroll = max(0, self.total_lines - self.body_height)
            elif key == curses.KEY_END:
                self.follow = True
                self.scroll = 0
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                inp.backspace()
            elif key == curses.KEY_DC:
                inp.delete()
            elif key == curses.KEY_LEFT:
                inp.left()
            elif key == curses.KEY_RIGHT:
                inp.right()
            elif key in (curses.KEY_UP,):
                inp.hist_up()
            elif key in (curses.KEY_DOWN,):
                inp.hist_down()
            return True
        # printable/control characters arrive as one-char strings via get_wch
        if key in ("\n", "\r"):
            self.submit()
        elif key in ("\x03", "\x04"):  # Ctrl-C / Ctrl-D
            return False
        elif key == "\x01":  # Ctrl-A
            inp.home()
        elif key == "\x05":  # Ctrl-E
            inp.end()
        elif key == "\x15":  # Ctrl-U
            inp.kill_before()
        elif key == "\x0b":  # Ctrl-K
            inp.kill_after()
        elif key == "\x17":  # Ctrl-W
            inp.kill_word()
        elif key == "\t":
            inp.insert("    ")
        elif key >= " " and key != "\x7f":
            inp.insert(key)
            self.ping_typing()
        return True

    def ping_typing(self, force=False):
        now = time.time()
        active = bool(self.input.text)
        if not active:
            if self._typing_on:
                self.store.set_typing(self.nick, False)
                self._typing_on = False
            return
        if not force and now - self.last_type_ping < TYPING_REFRESH_SECONDS:
            return
        self.last_type_ping = now
        self.store.set_typing(self.nick, True)
        self._typing_on = True

    # -------------------------------------------------------------- updates

    def poll_updates(self):
        batch, reset = self.store.poll()
        if reset:
            self.messages = list(batch)
            batch = []
        elif batch:
            self.messages.extend(batch)
        if len(self.messages) > MAX_RENDERED_RECORDS:
            del self.messages[:-MAX_RENDERED_RECORDS]
        if batch:
            self.alerts(batch)

        now = time.time()
        if now - self.last_beat >= HEARTBEAT_SECONDS:
            self.store.heartbeat(self.nick)
            self.last_beat = now

        online, typing = self.store.online_users()
        current = set(online)
        if self.known_online is None:
            self.known_online = current
        else:
            for name in sorted(current - self.known_online, key=str.lower):
                self.note(f"{name} joined")
            for name in sorted(self.known_online - current, key=str.lower):
                self.note(f"{name} left")
            self.known_online = current
        self.online_names = online
        self.typing_names = typing
        if len(self.notes) > 200:
            del self.notes[:-200]

    def alerts(self, batch):
        for record in batch:
            if record.get("type") not in ("message", "action"):
                continue
            user = str(record.get("user", ""))
            text = str(record.get("text", ""))
            if user != self.nick and self.mention_rx.search(text):
                self.set_status(f"{user} mentioned you")
                self.flash_pending = True
        if not self.follow:
            self.beep_pending = True

    def note(self, text):
        self.notes.append({"type": "note", "ts": time.time(), "text": text})

    def set_status(self, text, seconds=3.0):
        self.status = text
        self.status_until = time.time() + seconds

    # -------------------------------------------------------------- commands

    def submit(self):
        text = self.input.submit()
        self.store.set_typing(self.nick, False)
        self._typing_on = False
        if not text:
            return
        if text.startswith("/"):
            self.command(text)
            return
        try:
            self.store.append_message(self.nick, text[:MAX_MESSAGE_LEN])
        except OSError as exc:
            self.set_status(f"Send failed: {exc}")
        self.follow = True
        self.scroll = 0

    def command(self, text):
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "/quit":
            raise SystemExit(0)
        handlers = {
            "/help": self.cmd_help,
            "/clear": self.cmd_clear,
            "/color": self.cmd_color,
            "/me": self.cmd_me,
            "/nick": self.cmd_nick,
            "/rooms": self.cmd_rooms,
            "/room": self.cmd_room,
            "/reply": self.cmd_reply,
            "/react": self.cmd_react,
        }
        handler = handlers.get(cmd)
        if handler is None:
            self.set_status(f"Unknown command {cmd} — /help")
            return
        handler(arg)

    def cmd_help(self, arg):
        for line in HELP_LINES:
            self.note(line)

    def cmd_clear(self, arg):
        try:
            self.store.clear()
        except OSError as exc:
            self.set_status(f"Clear failed: {exc}")
            return
        self.messages = []
        self.note("room history cleared (everyone)")

    def cmd_color(self, arg):
        color = normalize_hex_color(arg)
        if not color:
            self.set_status("Use /color #6f8f7a")
            return
        try:
            self.store.append_color(self.nick, color)
            self.set_status(f"Color set to {color}")
        except OSError as exc:
            self.set_status(f"Color failed: {exc}")

    def cmd_me(self, arg):
        if not arg:
            self.set_status("Use /me waves hello")
            return
        try:
            self.store.append_action(self.nick, arg[:MAX_MESSAGE_LEN])
            self.follow = True
            self.scroll = 0
        except OSError as exc:
            self.set_status(f"Send failed: {exc}")

    def cmd_nick(self, arg):
        if not NICK_RE.match(arg):
            self.set_status("Use /nick name (letters, digits, . _ -, max 32)")
            return
        old = self.nick
        self.store.set_typing(old, False)
        self.nick = arg
        self.mention_rx = mention_pattern(arg)
        self.store.heartbeat(arg)
        self.note(f"you are now known as {arg}")

    def cmd_rooms(self, arg):
        try:
            rooms = self.store.list_rooms()
        except OSError as exc:
            self.set_status(f"List failed: {exc}")
            return
        self.note("rooms: " + (", ".join(rooms) if rooms else "(none yet)"))

    def cmd_room(self, arg):
        if not arg or " " in arg:
            self.set_status("Use /room name")
            return
        self.store.set_typing(self.nick, False)
        self._typing_on = False
        try:
            self.store = ChatStore(self.store.base_dir, arg)
        except OSError as exc:
            self.set_status(f"Switch failed: {exc}")
            return
        self.messages = []
        self.known_online = None
        self.follow = True
        self.scroll = 0
        self.store.heartbeat(self.nick)
        self.last_beat = time.time()
        self.note(f"switched to room {self.store.room}")

    def _resolve_number(self, token):
        try:
            number = int(token)
        except ValueError:
            return None
        return self.number_map.get(number)

    def cmd_reply(self, arg):
        parts = arg.split(maxsplit=1)
        if len(parts) != 2:
            self.set_status("Use /reply 3 nice point")
            return
        target = self._resolve_number(parts[0])
        if target is None:
            self.set_status("No such message number (see its first line order)")
            return
        try:
            self.store.append_message(self.nick, parts[1][:MAX_MESSAGE_LEN], reply_to=target)
            self.follow = True
            self.scroll = 0
        except OSError as exc:
            self.set_status(f"Send failed: {exc}")

    def cmd_react(self, arg):
        parts = arg.split(maxsplit=1)
        if not parts or not parts[0]:
            self.status = "Use /react 3 👍"
            return
        target = self._resolve_number(parts[0])
        if target is None:
            self.set_status("No such message number")
            return
        emoji = parts[1].strip() if len(parts) > 1 else REACTION_DEFAULT
        if not emoji or len(emoji) > 16 or emoji.startswith("/"):
            self.set_status("That emoji looks wrong")
            return
        try:
            self.store.append_reaction(self.nick, target, emoji)
        except OSError as exc:
            self.set_status(f"Send failed: {exc}")

    # ----------------------------------------------------------------- draw

    def draw_frame(self):
        stdscr = self.stdscr
        height, width = stdscr.getmaxyx()
        min_ok = height >= 8 and width >= 36
        stdscr.erase()
        if not min_ok:
            add_line(stdscr, 0, 0, "Make the terminal at least 36x8.", width)
            stdscr.refresh()
            return

        lines, number_map = self.renderer.render(self.messages, self.notes, width)
        self.number_map = number_map
        self.total_lines = len(lines)
        self.body_height = height - 3

        title = f" confchat v{__version__} | room: {self.store.room} | nick: {self.nick}"
        if self.online_names:
            title += f" | online: {', '.join(self.online_names)}"
        add_line(stdscr, 0, 0, title.ljust(width), width, curses.color_pair(1) | curses.A_BOLD)

        footer = " Enter send | Up/Down history | PgUp/PgDn scroll | /help commands "
        add_line(stdscr, height - 2, 0, footer.ljust(width), width, curses.color_pair(2))

        prompt = "> "
        input_width = max(1, width - len(prompt) - 1)
        visible, view_start = self.input.view(input_width)
        add_line(stdscr, height - 1, 0, prompt, width, curses.A_BOLD)
        add_line(stdscr, height - 1, len(prompt), visible, input_width)
        try:
            stdscr.move(height - 1, len(prompt) + (self.input.cursor - view_start))
        except curses.error:
            pass

        if self.follow:
            start = max(0, self.total_lines - self.body_height)
        else:
            start = max(0, self.total_lines - self.body_height - self.scroll)
        visible_rows = lines[start : start + self.body_height]
        blank_lines = self.body_height - len(visible_rows)
        for row in range(blank_lines):
            add_line(stdscr, 1 + row, 0, "", width)
        for row, (text, pair, flags) in enumerate(visible_rows, start=1 + blank_lines):
            attr = (curses.color_pair(pair) if pair else 0) | flags
            add_line(stdscr, row, 0, f" {text}".ljust(width - 1), width - 1, attr)

        side = ""
        if self.status:
            side = f" {self.status} "
        elif self.typing_names:
            side = f" {', '.join(self.typing_names)} typing... "
        if side:
            add_line(stdscr, height - 2, max(0, width - len(side) - 1), side, width, curses.color_pair(3))

        stdscr.refresh()
        if self.flash_pending:
            self.flash_pending = False
            try:
                curses.flash()
            except curses.error:
                pass
        if self.beep_pending:
            self.beep_pending = False
            try:
                curses.beep()
            except curses.error:
                pass


def run(store, nick):
    app = App(store, nick)
    try:
        curses.wrapper(app.run)
    except SystemExit:
        pass
