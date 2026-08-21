import json
import os
import time

import pytest

from confchat import store as store_mod
from confchat.store import ChatStore


@pytest.fixture()
def base(tmp_path):
    return str(tmp_path)


def test_room_name_sanitizes():
    assert store_mod.room_name("My Room!") == "My_Room_"
    assert store_mod.room_name("  ") == "lobby"
    assert store_mod.room_name("") == "lobby"
    assert len(store_mod.room_name("x" * 500)) == 80
    assert store_mod.room_name("a-b_c.d") == "a-b_c.d"


def test_append_load_roundtrip(base):
    s = ChatStore(base, "lobby")
    s.append_message("alice", "hello")
    records = s.load()
    assert len(records) == 1
    record = records[0]
    assert record["type"] == "message"
    assert record["user"] == "alice"
    assert record["text"] == "hello"
    assert record["uid"] == os.getuid()
    assert record["id"]
    assert isinstance(record["ts"], float)


def test_message_length_cap(base):
    s = ChatStore(base, "lobby")
    s.append_message("alice", "x" * 5000)
    assert len(s.load()[0]["text"]) == 2000


def test_poll_returns_only_new_records(base):
    s = ChatStore(base, "lobby")
    assert s.poll() == ([], False)
    s.append_message("alice", "one")
    s.append_message("bob", "two")
    batch, reset = s.poll()
    assert reset is False
    assert [r["text"] for r in batch] == ["one", "two"]
    assert s.poll() == ([], False)


def test_poll_handles_partial_line(base):
    s = ChatStore(base, "lobby")
    with s.log_path.open("ab") as f:
        f.write(b'{"ts": 1.0, "user": "x", "uid": 1, "type": "message", "tex')
    assert s.poll() == ([], False)
    with s.log_path.open("ab") as f:
        f.write(b't": "hi"}\n')
    batch, _ = s.poll()
    assert [r["text"] for r in batch] == ["hi"]


def test_poll_resets_after_external_truncation(base):
    s = ChatStore(base, "lobby")
    s.append_message("alice", "gone soon")
    s.load()
    with s.log_path.open("wb"):
        pass
    batch, reset = s.poll()
    assert reset is True
    assert batch == []
    s.append_message("bob", "fresh")
    batch, reset = s.poll()
    assert reset is False
    assert [r["text"] for r in batch] == ["fresh"]


def test_rotation_keeps_recent_lines(base, monkeypatch):
    monkeypatch.setattr(store_mod, "MAX_LOG_BYTES", 300)
    monkeypatch.setattr(store_mod, "ROTATE_KEEP_BYTES", 120)
    s = ChatStore(base, "lobby")
    s.load()  # prime incremental-read state so the rewrite shows up as a reset
    for i in range(6):
        s.append_message("alice", f"message number {i} with some padding text")
    size = s.log_path.stat().st_size
    assert size < 600  # rotated down from ~800 bytes of records
    # our offset went stale after the rewrite; poll must self-heal by reloading
    records, reset = s.poll()
    assert reset is True
    assert 1 <= len(records) <= 4
    assert records[-1]["text"].startswith("message number")


def test_clear_truncates_shared_log(base):
    s = ChatStore(base, "lobby")
    s.append_message("alice", "bye")
    s.clear()
    assert s.log_path.stat().st_size == 0
    assert s.load() == []


def test_decode_legacy_and_bad_lines(base):
    s = ChatStore(base, "lobby")
    lines = [
        json.dumps({"ts": 2.0, "user": "bob", "text": "/me waves"}),
        json.dumps({"ts": 3.0, "user": "bob", "color": "#ff0000"}),
        "not json at all",
        json.dumps({"user": "no-ts", "text": "dropped"}),
        json.dumps({"ts": 4.0, "user": "carol", "text": "plain legacy"}),
    ]
    with s.log_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    records = s.load()
    by_ts = {r["ts"]: r for r in records}
    assert by_ts[2.0]["type"] == "action"
    assert by_ts[2.0]["text"] == "waves"
    assert by_ts[3.0]["type"] == "color"
    assert by_ts[4.0]["type"] == "message"
    assert len(records) == 3


def test_reaction_records_roundtrip(base):
    s = ChatStore(base, "lobby")
    s.append_reaction("alice", "target-1", "\U0001F44D")
    record = s.load()[0]
    assert record["type"] == "reaction"
    assert record["target"] == "target-1"


def test_presence_online_and_typing(base):
    s = ChatStore(base, "lobby")
    s.uid = 1000
    s.heartbeat("alice")
    s.uid = 2001
    s.heartbeat("Bob")
    online, typing = s.online_users()
    assert online == ["alice", "Bob"]  # sorted case-insensitively
    assert typing == []
    s.set_typing("alice", True)
    _, typing = s.online_users()
    assert typing == ["alice"]
    s.set_typing("alice", False)
    _, typing = s.online_users()
    assert typing == []


def test_presence_expires_stale_markers(base):
    s = ChatStore(base, "lobby")
    s.heartbeat("ghost")
    marker = next(s.presence_dir.glob("*ghost.json"))
    old = time.time() - store_mod.PRESENCE_TIMEOUT - 10
    os.utime(marker, (old, old))
    online, _ = s.online_users()
    assert online == []
    assert not list(s.presence_dir.glob("*ghost.json"))


def test_presence_same_uid_renaming_keeps_freshest(base):
    s = ChatStore(base, "lobby")
    s.heartbeat("alice")
    time.sleep(0.01)
    s.heartbeat("alicia")  # same uid, new nick: freshest file wins
    online, _ = s.online_users()
    assert online == ["alicia"]


def test_list_rooms(base):
    ChatStore(base, "lobby")
    ChatStore(base, "friends")
    other = ChatStore(base, "friends")
    assert sorted(other.list_rooms()) == ["friends", "lobby"]
