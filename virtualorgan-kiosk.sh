#!/bin/bash
# Called (via sudo, NOPASSWD, see /etc/sudoers.d/virtualorgan) by the
# unprivileged virtualorgan Flask app whenever the "Kiosk-Modus" checkbox in
# Einstellungen changes, so the toggle takes effect immediately instead of
# only after the next reboot.
set -e
FLAG=/opt/virtualorgan/data/kiosk_enabled

case "$1" in
  start)
    touch "$FLAG"
    chown virtualorgan:virtualorgan "$FLAG"
    systemctl restart virtualorgan-kiosk.service
    ;;
  stop)
    rm -f "$FLAG"
    systemctl stop virtualorgan-kiosk.service
    ;;
  *)
    echo "Usage: $0 {start|stop}" >&2
    exit 1
    ;;
esac
