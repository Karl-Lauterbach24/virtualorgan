import struct
import functools


@functools.lru_cache(maxsize=4)
def list_presets(sf2_path):
    """Returns [{"bank":, "program":, "name":}] parsed straight from the
    SoundFont's PHDR chunk -- no extra pip dependency needed (those have
    broken the first-boot install once already)."""
    try:
        with open(sf2_path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    idx = data.find(b"phdr")
    if idx == -1:
        return []
    size = struct.unpack_from("<I", data, idx + 4)[0]
    start = idx + 8
    count = size // 38
    presets = []
    for i in range(max(0, count - 1)):  # last record is a terminal sentinel
        rec = data[start + i * 38: start + (i + 1) * 38]
        if len(rec) < 24:
            continue
        name = rec[0:20].split(b"\x00")[0].decode("latin-1", "ignore").strip()
        preset, bank = struct.unpack_from("<HH", rec, 20)
        if name:
            presets.append({"bank": bank, "program": preset, "name": name})
    return sorted(presets, key=lambda p: (p["bank"], p["program"]))


@functools.lru_cache(maxsize=4)
def _preset_set(sf2_path):
    return {(p["bank"], p["program"]) for p in list_presets(sf2_path)}


def resolve_preset(sf2_path, bank, program):
    """Map a requested (bank, program) to one that actually exists in the
    loaded SoundFont, with a sane fallback chain, instead of leaving it to
    fluidsynth -- which simply keeps whatever was previously selected when
    program_select() is asked for a bank/program pair the SoundFont doesn't
    have, with zero indication anything went wrong.

    This is exactly what happens with a lot of real-world organ MIDI files:
    they were authored for some other instrument's bank layout, so they
    send a bank-select value (e.g. from a Hauptwerk/GS/XG export) that means
    nothing to this SoundFont, while the *program* number underneath is
    often still a perfectly valid, meaningful register in bank 0. Falling
    back to "same program, bank 0" recovers exactly that in practice.

    Order tried: exact match -> same program in bank 0 -> program 0 in the
    lowest available bank -> the (bank, program) unchanged if the SoundFont
    couldn't be read at all (nothing to validate against).
    """
    presets = _preset_set(sf2_path)
    if not presets:
        return bank, program
    if (bank, program) in presets:
        return bank, program
    if (0, program) in presets:
        return 0, program
    fallback_bank = min(b for b, _ in presets)
    if (fallback_bank, 0) in presets:
        return fallback_bank, 0
    return min(presets)
