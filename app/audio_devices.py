"""Enumerates available audio output targets for the dropdown menu."""
import subprocess


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return ""


def list_alsa_outputs():
    """Klinke (analog jack), HDMI, USB-DACs etc. via `aplay -L`.
    Parsed line-by-line (name = unindented line, description = following
    indented lines) since blank-line separation between entries is not
    guaranteed on every system."""
    out = _run(["aplay", "-L"])
    lines = out.splitlines()
    devices = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line or line[0].isspace():
            i += 1
            continue
        name = line.strip()
        desc = ""
        j = i + 1
        while j < len(lines) and lines[j][:1].isspace():
            if not desc and lines[j].strip():
                desc = lines[j].strip()
            j += 1
        i = j
        # Nur ein sauberer, automatisch konvertierender Eintrag pro Karte
        if not name.startswith("default:CARD="):
            continue
        label = desc or name
        if "hdmi" in name.lower() or "hdmi" in desc.lower():
            label = f"HDMI – {desc}"
        elif "headphones" in desc.lower() or "bcm2835" in name.lower():
            label = f"Klinke (3.5mm) – {desc}"
        devices.append({"id": f"alsa:{name}", "label": label})
    return devices


def list_bluetooth_outputs():
    """Paired/connected Bluetooth audio sinks via bluetoothctl."""
    out = _run(["bluetoothctl", "devices", "Paired"])
    devices = []
    for line in out.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) == 3:
            mac, _, name = parts
            devices.append({"id": f"bluetooth:{mac}", "label": f"Bluetooth – {name}"})
    return devices


def list_network_outputs():
    """Network audio via PulseAudio RTP / remote sinks (pactl)."""
    out = _run(["pactl", "list", "short", "sinks"])
    devices = []
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2 and ("rtp" in cols[1].lower() or "remote" in cols[1].lower()):
            devices.append({"id": f"network:{cols[1]}", "label": f"Netzwerk – {cols[1]}"})
    return devices


def list_outputs():
    devices = [{"id": "web:browser", "label": "Web-Browser (Vorhören)"}]
    devices += list_alsa_outputs()
    devices += list_bluetooth_outputs()
    devices += list_network_outputs()
    return devices


def list_midi_inputs():
    """Physical/virtual MIDI input ports via mido+rtmidi."""
    try:
        import mido
        return mido.get_input_names()
    except Exception:
        return []
