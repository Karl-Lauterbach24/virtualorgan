# Virtual Organ – Armbian Image (Raspberry Pi 4B)

## What's Included

The image already contains the complete web application under `/opt/virtualorgan/`
(Flask + FluidSynth engine) along with the included `Organ.sf2` soundfont. Since
this image was not built on a real Raspberry Pi (no network/chroot available in
this environment), the system completes its installation **during the very first
boot on actual hardware** using `virtualorgan-firstboot.service`.

The following components are installed automatically:

* FluidSynth
* Python virtual environment
* Chromium
* Cage (kiosk compositor)
* Bluetooth
* PulseAudio

Depending on your internet connection, this process takes approximately **2–5 minutes**. Once finished, the web server starts automatically and remains active.

**Requirement:** The Raspberry Pi must have an internet connection during the first boot (Ethernet is recommended). Wi-Fi can be configured beforehand using Armbian's `armbian-config` or `/boot/armbian_first_run.txt`.

## Access

* Web interface: `http://virtualorgan.local:5000` (hostname is preconfigured)
* Default login: `admin` / `virtualorgan` (please change this after the first login. A dedicated password change page can easily be added; currently you can create a new administrator under **Settings → Users** and then remove the default admin account.)
* Authentication is **disabled by default** (open access) and can be enabled in **Settings**.

## Features

* **Library:** Upload MIDI, `.kar`, and MusicXML files, organize them into folders, and store them either as shared files or per-user libraries.
* **Playback:** Uses FluidSynth with `Organ.sf2`. Audio output can be routed to the web browser (preview), 3.5 mm audio jack, HDMI, Bluetooth, or network audio (PulseAudio).
* **Multiple MIDI controllers:** Connect multiple external MIDI devices simultaneously and freely assign them to individual channels/voices (`/midi`).
* **Registration management:** Save per-song voice assignments (channel → program), create copies of registrations, save/load global profiles, and automatically reset newly loaded voices to their default values.
* **Multi-user support:** Role-based access (admin/user) with optional login enforcement.
* **HDMI kiosk mode:** Full-screen Chromium running under Cage, can be enabled or disabled.
* **Boot optimizations:** Bluetooth socket activation, disabled startup delays (`NetworkManager-wait-online`, `apt-daily` timers), and a quieter boot screen.

## Directory Structure

```text
/opt/virtualorgan/app/          Flask application (app.py, synth_engine.py, ...)
 /opt/virtualorgan/soundfonts/   Organ.sf2
/opt/virtualorgan/library/      Uploaded files (shared/, users/<username>/)
/opt/virtualorgan/data/         SQLite database, kiosk mode flag
/etc/systemd/system/            virtualorgan*.service units
/usr/local/sbin/virtualorgan-firstboot.sh
```
