"""Append-only JSONL chat storage shared between local users.

Every room is a single JSONL file plus a lock file. Writers serialize with
flock; readers track their byte offset so each poll only parses new lines.
Presence (who is online / typing) lives in small per-user marker files whose
mtimes act as heartbeats, so it never bloats the message log.
"""

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

DEFAULT_DATA_DIR = "/var/tmp/confchat-data"

# Rotation: rooms never grow unbounded. When the log passes MAX_LOG_BYTES it
# is rewritten down to roughly ROTATE_KEEP_BYTES of the newest complete lines.
MAX_LOG_BYTES = 2 * 1024 * 1024
ROTATE_KEEP_BYTES = 512 * 1024

PRESENCE_TIMEOUT = 15.0
TYPING_TIMEOUT = 5.0

_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")


def room_name(value):
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in str(value).strip())
    return safe[:80] or "lobby"


def safe_name(value):
    """Filesystem-safe form of a nick for presence marker files."""
    cleaned = _UNSAFE_NAME_RE.sub("_", str(value))[:40]
    return cleaned or "user"


def ensure_shared(path, directory=False):
    if directory:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o1777)
    elif path.exists():
        os.chmod(path, 0o666)


class ChatStore:
    def __init__(self, base_dir, room):
        self.base_dir = Path(base_dir)
        ensure_shared(self.base_dir, directory=True)
        self.room = room_name(room)
        digest = hashlib.sha1(self.room.encode("utf-8")).hexdigest()[:10]
        self.log_path = self.base_dir / f"{self.room}-{digest}.jsonl"
        self.lock_path = self.base_dir / f"{self.room}-{digest}.lock"
        self.presence_dir = self.base_dir / "online" / f"{self.room}-{digest}"
        self.log_path.touch(exist_ok=True)
        self.lock_path.touch(exist_ok=True)
        ensure_shared(self.log_path)
        ensure_shared(self.lock_path)
        ensure_shared(self.presence_dir.parent, directory=True)
        ensure_shared(self.presence_dir, directory=True)
        self.uid = os.getuid()
        self._offset = 0
        self._ino = None
        self._partial = b""

    # ------------------------------------------------------------------ write

    def append_message(self, user, text, reply_to=None):
        record = self._base_record(user)
        record["type"] = "message"
        record["text"] = str(text)[:2000]
        if reply_to:
            record["reply_to"] = reply_to
        self._append_record(record)

    def append_action(self, user, text):
        record = self._base_record(user)
        record["type"] = "action"
        record["text"] = str(text)[:2000]
        self._append_record(record)

    def append_color(self, user, color):
        record = self._base_record(user)
        record["type"] = "color"
        record["color"] = color
        self._append_record(record)

    def append_reaction(self, user, target, emoji):
        record = self._base_record(user)
        record["type"] = "reaction"
        record["target"] = target
        record["emoji"] = emoji
        self._append_record(record)

    def _base_record(self, user):
        return {
            "id": f"{time.time_ns()}-{os.getpid()}",
            "ts": time.time(),
            "user": user,
            "uid": self.uid,
        }

    def _append_record(self, record):
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        with self.lock_path.open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self._rotate_if_needed()
                with self.log_path.open("a", encoding="utf-8") as log:
                    log.write(line)
                    log.flush()
                    os.fsync(log.fileno())
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _rotate_if_needed(self):
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return
        if size <= MAX_LOG_BYTES:
            return
        try:
            with self.log_path.open("rb") as f:
                f.seek(size - ROTATE_KEEP_BYTES)
                tail = f.read()
            newline = tail.find(b"\n")
            if newline >= 0:
                tail = tail[newline + 1 :]
            fd, tmp = tempfile.mkstemp(dir=str(self.base_dir), prefix=".rotate-")
            with os.fdopen(fd, "wb") as out:
                out.write(tail)
            os.chmod(tmp, 0o666)
            os.replace(tmp, self.log_path)
        except OSError:
            # Rotation is best effort; a failed rewrite must not lose chat.
            pass

    def clear(self):
        """Truncate the shared room history for everyone."""
        with self.lock_path.open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with self.log_path.open("w"):
                    pass
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        self._offset = 0
        self._partial = b""

    # ------------------------------------------------------------------ read

    @staticmethod
    def _decode(raw):
        if not raw or not raw.strip():
            return None
        try:
            item = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(item, dict) or not isinstance(item.get("ts"), (int, float)):
            return None
        user = item.get("user")
        if not isinstance(user, str) or not user:
            return None
        kind = item.get("type")
        if kind == "message" and isinstance(item.get("text"), str):
            if item["text"].startswith("/me "):  # legacy actions stored as messages
                item["type"] = "action"
                item["text"] = item["text"][4:]
            return item
        if kind == "action" and isinstance(item.get("text"), str):
            return item
        if kind == "color" and isinstance(item.get("color"), str):
            return item
        if (
            kind == "reaction"
            and isinstance(item.get("target"), str)
            and isinstance(item.get("emoji"), str)
        ):
            return item
        if kind is None:  # records written by confchat <= 0.1
            if isinstance(item.get("text"), str):
                if item["text"].startswith("/me "):
                    item["type"] = "action"
                    item["text"] = item["text"][4:]
                else:
                    item["type"] = "message"
                return item
            if isinstance(item.get("color"), str):
                item["type"] = "color"
                return item
        return None

    def load(self):
        """Full history; also resyncs incremental-read state."""
        records = []
        try:
            data = self.log_path.read_bytes()
            self._ino = self.log_path.stat().st_ino
        except OSError:
            self._offset = 0
            self._partial = b""
            return records
        self._offset = len(data)
        self._partial = b""
        for raw in data.split(b"\n"):
            record = self._decode(raw)
            if record:
                records.append(record)
        return records

    def poll(self):
        """Return (new_records_since_last_call, full_reset)."""
        try:
            stat = self.log_path.stat()
        except OSError:
            self._offset = 0
            self._partial = b""
            return [], False
        if self._ino is None:
            self._ino = stat.st_ino
        if stat.st_ino != self._ino or stat.st_size < self._offset:
            return self.load(), True  # rotated or truncated: resync from scratch
        if stat.st_size == self._offset:
            return [], False
        with self.log_path.open("rb") as f:
            f.seek(self._offset)
            chunk = f.read()
        self._offset += len(chunk)
        parts = (self._partial + chunk).split(b"\n")
        self._partial = parts.pop()
        return [r for r in map(self._decode, parts) if r], False

    def list_rooms(self):
        names = set()
        for path in self.base_dir.glob("*.jsonl"):
            stem = path.stem  # "<room>-<10 hex digest>"
            names.add(stem[:-11] if len(stem) > 11 else stem)
        return sorted(names)

    # -------------------------------------------------------------- presence

    def _presence_paths(self, nick):
        base = safe_name(nick)
        return (
            self.presence_dir / f"{self.uid}.{base}.json",
            self.presence_dir / f"{self.uid}.{base}.typing",
        )

    def heartbeat(self, nick):
        """Refresh this client's online marker (mtime doubles as timestamp)."""
        path, _ = self._presence_paths(nick)
        try:
            self.presence_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps({"user": nick, "uid": self.uid}), encoding="utf-8")
            os.chmod(tmp, 0o666)
            os.replace(tmp, path)
        except OSError:
            pass

    def set_typing(self, nick, typing):
        _, path = self._presence_paths(nick)
        try:
            if typing:
                tmp = path.with_suffix(f".tmp{os.getpid()}")
                tmp.write_text(json.dumps({"user": nick}), encoding="utf-8")
                os.chmod(tmp, 0o666)
                os.replace(tmp, path)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass

    def online_users(self):
        """Return (online nicks, currently-typing nicks), stale markers pruned."""
        now = time.time()
        people = {}
        typing = set()
        try:
            entries = sorted(self.presence_dir.iterdir())
        except OSError:
            return [], []
        for path in entries:
            name = path.name
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if name.endswith(".typing"):
                if age <= TYPING_TIMEOUT:
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                        if isinstance(data.get("user"), str) and data["user"]:
                            typing.add(data["user"])
                    except (OSError, ValueError):
                        pass
                else:
                    self._unlink(path)
                continue
            if not name.endswith(".json"):
                continue
            if age > PRESENCE_TIMEOUT:
                self._unlink(path)
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                uid = int(name.split(".", 1)[0])
                nick = data.get("user")
            except (OSError, ValueError, IndexError):
                continue
            if not isinstance(nick, str) or not nick:
                continue
            previous = people.get(uid)
            if previous is None or age < previous[0]:
                people[uid] = (age, nick)
        online = sorted((nick for _, nick in people.values()), key=str.lower)
        return online, sorted(typing, key=str.lower)

    @staticmethod
    def _unlink(path):
        try:
            path.unlink()
        except OSError:
            pass  # sticky world-writable dir: only the owner can prune
