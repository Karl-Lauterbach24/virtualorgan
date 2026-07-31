#!/bin/bash
# Called (via sudo, NOPASSWD, see /etc/sudoers.d/virtualorgan) by the
# unprivileged virtualorgan Flask app when the hostname is changed in
# Einstellungen. Validates the input itself since sudoers can't do that.
set -e
NAME="$1"

if [[ ! "$NAME" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$ ]]; then
  echo "Ungültiger Hostname: $NAME" >&2
  exit 1
fi

hostnamectl set-hostname "$NAME"
if grep -q "^127.0.1.1" /etc/hosts; then
  sed -i "s/^127.0.1.1.*/127.0.1.1\t$NAME/" /etc/hosts
else
  echo -e "127.0.1.1\t$NAME" >> /etc/hosts
fi
