# confchat

Realtime TUI chat for users on the same machine. No server, no network: rooms
are append-only JSONL files in a shared directory, synchronized with `flock`.

```
 ┌ confchat v0.2.0 | room: lobby | nick: teeqo | online: alice, teeqo ─┐
 │  ───────────── Friday, Aug 21 2026 ─────────────                    │
 │  09:12 alice: anyone up for lunch?                                  │
 │  09:12 * alice is starving                                          │
 │  09:13 teeqo: yes — /react 1 👍                                     │
 │   👍 alice/teeqo                                                    │
 │   · bob joined                                                      │
 └ Enter send | Up/Down history | PgUp/PgDn scroll | /help commands    ┘
 > _
```

## Install

From a checkout (no install step):

```sh
./confchat
```

With pipx (recommended for a permanent install):

```sh
pipx install /path/to/confchat        # or: pipx install git+https://github.com/tanishqsrivastavaa/confchat
confchat --room friends --nick teeqo
```

For all local accounts to get the short command automatically, an admin can
link the checkout shim globally:

```sh
sudo ln -sf /path/to/confchat/confchat /usr/local/bin/confchat
```

## Usage

```sh
confchat                          # join the default "lobby" room
confchat --room friends           # shared room name
confchat --room friends --nick teeqo
```

### Commands

| Command           | Effect                                    |
| ----------------- | ----------------------------------------- |
| `/help`           | show command reference                    |
| `/color #6f8f7a`  | set your message color (shared)           |
| `/me waves hello` | send an action message                    |
| `/nick newname`   | change your display name                  |
| `/rooms`          | list rooms that exist on this machine     |
| `/room name`      | switch to another room without restarting |
| `/reply N text`   | reply to message number N                 |
| `/react N [emoji]`| react to message number N (default 👍)    |
| `/clear`          | clear room history **for everyone**       |
| `/quit`           | exit                                      |

Message numbers are the order of messages on screen (1 = oldest visible).

### Keys

- `Enter` send · `Up`/`Down` input history
- `Left`/`Right`, `Ctrl-A`/`Ctrl-E` move cursor; `Ctrl-U`/`Ctrl-K`/`Ctrl-W` kill line/word
- `PgUp`/`PgDn` scroll back; `Home` oldest; `End` jump to live
- Paste multi-line text safely (bracketed paste supported)

## How it works

- One JSONL file per room (`<room>-<hash>.jsonl`) plus a `.lock` file; writers
  serialize with `flock`, so any number of clients can chat concurrently.
- Clients read incrementally (byte offset per session), so long-running rooms
  stay fast.
- Logs rotate automatically around 2 MB down to ~512 KB of newest history.
- Presence ("online" and "typing...") uses tiny per-user marker files whose
  mtimes act as heartbeats; stale markers expire after ~15s / ~5s.
- Every record is stamped with the sender's OS uid. If two different uids use
  the same nick, they render as `alice·1000` vs `alice·1001` so impersonation
  is at least visible.

## Privacy notes

The data directory is world-readable/writable by design (any local account can
chat). That means:

- Anyone with a local account can read any room's history if they find the
  files. Do not use confchat for secrets.
- uid stamps are self-reported by clients, not enforced by the OS. They make
  spoofing obvious, not impossible.

## Development

```sh
uv venv && uv pip install pytest ruff   # or any Python >= 3.11
pytest -q
ruff check .
```

Layout: `src/confchat/store.py` (storage + presence), `tui.py` (curses UI),
`colors.py` (color management), `cli.py` (argparse entry). The root `confchat`
file is a runnable shim for checkouts.

## License

MIT
