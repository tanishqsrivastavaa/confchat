"""Color setup: user badge colors, custom hex overrides, terminal fallbacks."""

import curses
import re

USER_COLOR_START = 10
USER_COLOR_BASE = 100
USER_FG_COLOR = 90
CUSTOM_PAIR_START = 40
CUSTOM_COLOR_BASE = 120
MAX_CUSTOM_COLORS = 48

HEX_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")

USER_RGB_COLORS = (
    (120, 300, 230),
    (330, 280, 130),
    (150, 230, 390),
    (310, 210, 360),
    (110, 300, 330),
    (390, 190, 170),
    (270, 290, 320),
)
USER_256_COLORS = (
    23,
    58,
    60,
    95,
    24,
    88,
    238,
)
USER_BASIC_COLORS = (
    (curses.COLOR_WHITE, curses.COLOR_BLUE),
    (curses.COLOR_WHITE, curses.COLOR_MAGENTA),
    (curses.COLOR_BLACK, curses.COLOR_CYAN),
    (curses.COLOR_BLACK, curses.COLOR_GREEN),
    (curses.COLOR_WHITE, curses.COLOR_RED),
    (curses.COLOR_BLACK, curses.COLOR_YELLOW),
    (curses.COLOR_BLACK, curses.COLOR_WHITE),
)


def normalize_hex_color(value):
    match = HEX_COLOR_RE.match(str(value).strip())
    if not match:
        return None
    return f"#{match.group(1).lower()}"


def hex_to_rgb1000(color):
    raw = color.lstrip("#")
    return tuple(round(int(raw[idx : idx + 2], 16) * 1000 / 255) for idx in (0, 2, 4))


def hex_to_256(color):
    raw = color.lstrip("#")
    r, g, b = (int(raw[idx : idx + 2], 16) for idx in (0, 2, 4))
    if max(r, g, b) - min(r, g, b) < 10:
        gray = round((r / 255) * 23)
        return 232 + max(0, min(23, gray))
    levels = [0, 95, 135, 175, 215, 255]
    ri, gi, bi = (min(range(6), key=lambda idx: abs(channel - levels[idx])) for channel in (r, g, b))
    return 16 + (36 * ri) + (6 * gi) + bi


def color_state(records):
    """First-seen order gives each nick a default pair; /color records override."""
    users = []
    seen = set()
    for record in records:
        user = str(record.get("user", ""))
        if not user or user in seen:
            continue
        seen.add(user)
        users.append(user)
    defaults = {user: USER_COLOR_START + (idx % len(USER_RGB_COLORS)) for idx, user in enumerate(users)}
    overrides = {}
    for record in records:
        if record.get("type") != "color":
            continue
        color = normalize_hex_color(str(record.get("color", "")))
        if color:
            overrides[str(record.get("user", ""))] = color
    return defaults, overrides


class UserColorRegistry:
    def __init__(self):
        self.custom_pairs = {}

    def pair_for(self, fallback_pair, color):
        """Map a hex override to a curses pair; falls back when unsupported.

        Only touches curses when a custom color is actually requested, so the
        default path is safe to exercise headless (tests).
        """
        if not color:
            return fallback_pair
        if color in self.custom_pairs:
            return self.custom_pairs[color]
        if len(self.custom_pairs) >= MAX_CUSTOM_COLORS:
            return fallback_pair

        offset = len(self.custom_pairs)
        pair_id = CUSTOM_PAIR_START + offset
        if curses.has_colors() and curses.can_change_color() and curses.COLORS > CUSTOM_COLOR_BASE + offset:
            color_id = CUSTOM_COLOR_BASE + offset
            curses.init_color(color_id, *hex_to_rgb1000(color))
            curses.init_pair(pair_id, USER_FG_COLOR, color_id)
        elif curses.COLORS >= 256:
            curses.init_pair(pair_id, 250, hex_to_256(color))
        else:
            return fallback_pair
        self.custom_pairs[color] = pair_id
        return pair_id


def init_user_colors():
    if curses.has_colors() and curses.can_change_color() and curses.COLORS > USER_COLOR_BASE + len(USER_RGB_COLORS):
        curses.init_color(USER_FG_COLOR, 900, 880, 820)
        for offset, rgb in enumerate(USER_RGB_COLORS):
            color_id = USER_COLOR_BASE + offset
            curses.init_color(color_id, *rgb)
            curses.init_pair(USER_COLOR_START + offset, USER_FG_COLOR, color_id)
        return

    if curses.COLORS >= 256:
        for offset, bg in enumerate(USER_256_COLORS):
            curses.init_pair(USER_COLOR_START + offset, 250, bg)
        return

    for offset, (fg, bg) in enumerate(USER_BASIC_COLORS):
        curses.init_pair(USER_COLOR_START + offset, fg, bg)
