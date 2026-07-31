#!/bin/bash
# Virtual Organ – einmaliges Setup beim allerersten Boot (benötigt Internet).
set -e
LOG=/var/log/virtualorgan-firstboot.log
exec > >(tee -a "$LOG") 2>&1
echo "=== Virtual Organ First-Boot Setup: $(date) ==="

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  fluidsynth libfluidsynth-dev python3 python3-venv python3-pip python3-dev \
  build-essential libasound2-dev pkg-config curl sudo \
  alsa-utils pulseaudio pulseaudio-module-bluetooth bluez bluez-tools \
  chromium cage seatd \
  avahi-daemon libnss-mdns \
  fonts-dejavu-core

# --- Systembenutzer ------------------------------------------------------
id -u virtualorgan &>/dev/null || useradd -r -m -G audio,bluetooth,video,input -d /opt/virtualorgan virtualorgan
mkdir -p /opt/virtualorgan/data /opt/virtualorgan/library/shared /opt/virtualorgan/profiles
chown -R virtualorgan:virtualorgan /opt/virtualorgan

# --- Python-Umgebung ------------------------------------------------------
python3 -m venv /opt/virtualorgan/venv
PIP=/opt/virtualorgan/venv/bin/pip
$PIP install --quiet --upgrade pip
# Kern zuerst installieren: darf niemals am Fehlschlag eines Hardware-Pakets scheitern
$PIP install --quiet flask flask-login werkzeug numpy music21
# Audio/MIDI-Bindings einzeln, damit ein einzelner Build-Fehler nicht den ganzen Webserver lahmlegt
$PIP install --quiet mido || echo "WARN: mido-Installation fehlgeschlagen"
$PIP install --quiet python-rtmidi || echo "WARN: python-rtmidi-Installation fehlgeschlagen (Live-MIDI-Eingang nicht verfügbar)"
$PIP install --quiet pyFluidSynth || echo "WARN: pyFluidSynth-Installation fehlgeschlagen"
chown -R virtualorgan:virtualorgan /opt/virtualorgan

# --- Datenbank initialisieren ---------------------------------------------
sudo -u virtualorgan /opt/virtualorgan/venv/bin/python -c \
  "import sys; sys.path.insert(0,'/opt/virtualorgan/app'); import db; db.init_db()"

# --- Hostname --------------------------------------------------------------
hostnamectl set-hostname virtualorgan
sed -i "s/127.0.1.1.*/127.0.1.1\tvirtualorgan/" /etc/hosts 2>/dev/null || \
  echo -e "127.0.1.1\tvirtualorgan" >> /etc/hosts

# --- Kiosk-Flag (Standard: aktiv) -------------------------------------------
touch /opt/virtualorgan/data/kiosk_enabled
chown virtualorgan:virtualorgan /opt/virtualorgan/data/kiosk_enabled
loginctl enable-linger virtualorgan || true

# --- Helfer-Skripte + eingeschränkte sudo-Rechte für die Web-App -----------
# Die Web-App läuft als unprivilegierter Nutzer 'virtualorgan'; für Hostname-
# Änderung und Kiosk-Umschalten aus den Einstellungen heraus braucht sie
# punktuelle Root-Rechte für genau diese beiden Skripte (keine volle sudo-
# Freigabe, kein root-Webserver).
chmod 755 /usr/local/sbin/virtualorgan-kiosk-ctl.sh /usr/local/sbin/virtualorgan-set-hostname.sh
chown root:root /usr/local/sbin/virtualorgan-kiosk-ctl.sh /usr/local/sbin/virtualorgan-set-hostname.sh
if visudo -c -f /etc/sudoers.d/virtualorgan; then
  chmod 440 /etc/sudoers.d/virtualorgan
  chown root:root /etc/sudoers.d/virtualorgan
else
  echo "WARN: /etc/sudoers.d/virtualorgan ungültig, wird ignoriert (Kiosk-Umschalten/Hostname wirken erst nach Neustart)"
  rm -f /etc/sudoers.d/virtualorgan
fi

# --- Boot-Zeit-Optimierung ---------------------------------------------------
systemctl enable --now seatd.service 2>/dev/null || true        # von cage benötigt (VT/Input-Zugriff)
systemctl disable --now getty@tty1.service 2>/dev/null || true  # tty1 für Cage/Kiosk freigeben
systemctl enable --now bluetooth.service 2>/dev/null || true
systemctl mask NetworkManager-wait-online.service 2>/dev/null || true
systemctl disable e2scrub_reap.service 2>/dev/null || true
systemctl disable apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
sed -i 's/#\?console=serial0,[0-9]*//' /boot/armbianEnv.txt 2>/dev/null || true
sed -i 's/verbosity=[0-9]*/verbosity=0/' /boot/armbianEnv.txt 2>/dev/null || \
  echo "verbosity=0" >> /boot/armbianEnv.txt 2>/dev/null || true
grep -q "^bootlogo=" /boot/armbianEnv.txt 2>/dev/null && \
  sed -i 's/^bootlogo=.*/bootlogo=false/' /boot/armbianEnv.txt || \
  echo "bootlogo=false" >> /boot/armbianEnv.txt 2>/dev/null || true

# --- Dienste aktivieren ------------------------------------------------------
systemctl daemon-reload
systemctl enable --now virtualorgan.service
systemctl enable virtualorgan-kiosk.service

touch /opt/virtualorgan/data/.firstboot_done
systemctl disable virtualorgan-firstboot.service
echo "=== Setup abgeschlossen. Weboberfläche: http://virtualorgan.local:5000 ==="
