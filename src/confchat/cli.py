"""Command-line entry point for confchat."""

import argparse
import getpass
import signal

from . import __version__
from .store import DEFAULT_DATA_DIR, ChatStore
from .tui import run


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="confchat",
        description="Realtime TUI chat for users on the same machine.",
    )
    parser.add_argument("-r", "--room", default="lobby", help="room name, default: lobby")
    parser.add_argument("-n", "--nick", default="", help="display name, default: your login name")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="shared data directory")
    parser.add_argument("--version", action="version", version=f"confchat {__version__}")
    args = parser.parse_args(argv)

    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    nick = (args.nick.strip() or getpass.getuser())[:32]
    store = ChatStore(args.data_dir, args.room)
    try:
        run(store, nick)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
