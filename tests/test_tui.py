import curses
import time

from confchat.tui import InputLine, Renderer, fold_reactions, mention_pattern

DAY1 = time.mktime((2026, 8, 21, 9, 0, 0, 0, 0, -1))
DAY2 = time.mktime((2026, 8, 20, 9, 0, 0, 0, 0, -1))


def msg(number, text, user="alice", uid=1000, ts=None, **kw):
    record = {
        "id": f"id-{number}",
        "ts": ts if ts is not None else DAY1 + number,
        "user": user,
        "uid": uid,
        "type": "message",
        "text": text,
    }
    record.update(kw)
    return record


def render(records, notes=(), width=80, nick="me"):
    return Renderer(lambda: nick).render(list(records), list(notes), width)


def texts(lines):
    return [text for text, _, _ in lines]


def chat_texts(lines):
    """Display lines excluding date separators and note lines."""
    return [t for t in texts(lines) if "\u2500" not in t and "\u00b7" not in t]


# --------------------------------------------------------------------- input


def test_input_insert_and_cursor():
    inp = InputLine()
    inp.insert("hello")
    inp.left()
    inp.left()
    inp.insert("XY")
    assert inp.text == "helXYlo"
    assert inp.cursor == 5


def test_input_backspace_delete():
    inp = InputLine()
    inp.insert("abc")
    inp.left()
    inp.backspace()  # removes 'b'
    assert inp.text == "ac" and inp.cursor == 1
    inp.delete()  # removes 'c'
    assert inp.text == "a" and inp.cursor == 1
    inp.backspace()  # removes 'a'
    assert inp.text == "" and inp.cursor == 0
    inp.backspace()  # no-op at start
    inp.delete()  # no-op at end
    assert inp.text == ""


def test_input_kill_commands():
    inp = InputLine()
    inp.insert("hello world")
    inp.home()
    for _ in range(6):
        inp.right()  # cursor after "hello "
    inp.kill_before()
    assert inp.text == "world" and inp.cursor == 0
    inp.end()
    for _ in range(2):
        inp.left()
    inp.kill_after()
    assert inp.text == "wor" and inp.cursor == 3


def test_input_kill_word():
    inp = InputLine()
    inp.insert("foo bar wor")
    inp.kill_word()
    assert inp.text == "foo bar "
    assert inp.cursor == 8


def test_input_history_navigation():
    inp = InputLine()
    inp.insert("first message")
    assert inp.submit() == "first message"
    inp.insert("second message")
    assert inp.submit() == "second message"
    inp.hist_up()
    assert inp.text == "second message"
    inp.hist_up()
    assert inp.text == "first message"
    inp.hist_down()
    assert inp.text == "second message"
    inp.hist_down()
    assert inp.text == ""  # draft restored (was empty)
    inp.hist_up()  # recalls most recent entry
    inp.insert("x")  # editing a recalled entry
    assert inp.submit() == "second messagex"


def test_input_view_keeps_cursor_visible():
    inp = InputLine()
    inp.insert("0123456789abcdef")
    inp.end()
    visible, start = inp.view(6)
    assert visible == "bcdef" or visible.endswith("f")
    assert start + len(visible) - 1 <= inp.cursor
    inp.home()
    visible, start = inp.view(6)
    assert visible == "012345" and start == 0


# ------------------------------------------------------------------ reactions


def test_fold_reactions_toggle():
    records = [
        msg(1, "hi"),
        {"id": "r1", "ts": 1.0, "user": "bob", "uid": 2, "type": "reaction", "target": "id-1", "emoji": "\U0001F44D"},
        {"id": "r2", "ts": 2.0, "user": "cat", "uid": 3, "type": "reaction", "target": "id-1", "emoji": "\U0001F44D"},
        {"id": "r3", "ts": 3.0, "user": "bob", "uid": 2, "type": "reaction", "target": "id-1", "emoji": "\U0001F44D"},
    ]
    folded = fold_reactions(records)
    assert folded["id-1"]["\U0001F44D"] == ["cat"]


# ------------------------------------------------------------------- renderer


def test_render_wraps_long_messages():
    lines, numbers = render([msg(1, "word " * 60)])
    chat = chat_texts(lines)
    assert len(chat) > 1
    assert numbers == {1: "id-1"}
    assert chat[1].startswith(" " * len("09:00 alice: "))


def test_render_action_and_legacy_me():
    # legacy "/me " records are converted to actions by ChatStore._decode
    # (covered in test_store); the renderer only handles the action type.
    action = msg(1, "waves", type="action")
    assert "* alice waves" in chat_texts(render([action])[0])[0]


def test_render_date_separator_once_per_day():
    records = [
        msg(1, "yesterday", ts=DAY2),
        msg(2, "also yesterday", ts=DAY2 + 5),
        msg(3, "today"),
    ]
    separators = [t for t in texts(render(records)[0]) if "Aug" in t and "─" in t]
    assert len(separators) == 2
    assert "Aug 20" in separators[0] and "Aug 21" in separators[1]


def test_render_reply_quote():
    records = [msg(1, "original thought"), msg(2, "I agree", reply_to="id-1")]
    lines, _ = render(records)
    quote_index = next(i for i, t in enumerate(texts(lines)) if t.startswith("   > alice: original"))
    reply_index = next(i for i, t in enumerate(texts(lines)) if "I agree" in t)
    assert quote_index < reply_index


def test_render_reaction_line():
    records = [
        msg(1, "hot take"),
        {"id": "r1", "ts": 1.0, "user": "bob", "uid": 2, "type": "reaction", "target": "id-1", "emoji": "\U0001F525"},
    ]
    lines, _ = render(records)
    assert any("\U0001F525 bob" in t for t in texts(lines))


def test_render_mention_underlines_others_only():
    other = msg(1, "hey me, look at this")
    lines, _ = render([other])
    assert any(flags & curses.A_UNDERLINE for _, _, flags in lines)
    mine = msg(2, "hey me, look at this", user="me", uid=7)
    lines, _ = render([mine])
    assert not any(flags & curses.A_UNDERLINE for _, _, flags in lines)


def test_render_notes_are_dim_and_numbered_separately():
    note = {"type": "note", "ts": DAY1 + 2, "text": "bob joined"}
    lines, numbers = render([msg(1, "one"), msg(2, "two")], notes=[note])
    joined = [line for line in lines if "bob joined" in line[0]]
    assert joined and joined[0][2] & curses.A_DIM
    assert sorted(numbers) == [1, 2]


def test_render_disambiguates_uid_spoofing():
    records = [
        msg(1, "hello from real alice", user="alice", uid=1000),
        msg(2, "hello from fake alice", user="alice", uid=1001),
    ]
    lines, _ = render(records)
    body = "\n".join(texts(lines))
    assert "alice·1000:" in body
    assert "alice·1001:" in body


def test_mention_pattern_boundaries():
    pattern = mention_pattern("bob")
    assert pattern.search("hi Bob!")
    assert pattern.search("(bob)")
    assert not pattern.search("bobby")
    assert not pattern.search("abob")
