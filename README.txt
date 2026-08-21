confchat
========

Run from teeqo's shell on confessor:

  confchat

Use a shared room name:

  confchat --room friends

Pick a display name:

  confchat --room friends --nick teeqo

The app itself is installed at /var/tmp/confchat-app/confchat. A per-user
launcher is linked from ~/.cargo/bin/confchat. For all local accounts to get the
short command automatically, an admin can add this global symlink:

  sudo ln -sf /var/tmp/confchat-app/confchat /usr/local/bin/confchat

Keys and commands:

  Enter        send the current message
  PageUp       scroll up
  PageDown     scroll down
  End          jump back to live messages
  /color #hex  set your message bar color, for example /color #6f8f7a
  /me text     send an action message
  /quit        exit
  Ctrl-C       exit

Messages are stored in /var/tmp/confchat-data as append-only JSON lines. Anyone
with a local account can read messages in a room if they know or can list the
room files, so do not use it for secrets.
