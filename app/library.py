import os, json, shutil

import soundfont_info

BASE = "/opt/virtualorgan/library"
SHARED = os.path.join(BASE, "shared")
USERS = os.path.join(BASE, "users")
ALLOWED_EXT = {".mid", ".midi", ".kar", ".musicxml", ".mxl", ".xml"}


class InvalidPathError(ValueError):
    """Raised when a client-supplied relative path tries to escape its root."""


def user_dir(username):
    d = os.path.join(USERS, username)
    os.makedirs(d, exist_ok=True)
    return d


def _safe(rel):
    """Normalise a client-supplied relative path and guarantee it can never
    resolve outside of its scope root, however it's spelled ('..', absolute
    paths, backslashes, ...). Raises InvalidPathError instead of silently
    mangling the input, which is a much safer failure mode than a
    best-effort string replace."""
    rel = (rel or "").replace("\\", "/")
    normalized = os.path.normpath(rel).replace("\\", "/")
    if normalized in (".", ""):
        return ""
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or normalized.startswith("/"):
        raise InvalidPathError(f"Ungültiger Pfad: {rel!r}")
    return "/".join(parts)


def root_for(scope, username):
    return SHARED if scope == "shared" else user_dir(username)


def list_tree(scope, username):
    root = root_for(scope, username)
    tree = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        for f in sorted(filenames):
            if f.endswith(".json"):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext not in ALLOWED_EXT:
                continue
            full = os.path.join(dirpath, f)
            try:
                st = os.stat(full)
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                size, mtime = 0, 0
            tree.append({
                "name": f,
                "folder": rel_dir,
                "path": f"{rel_dir}/{f}" if rel_dir else f,
                "scope": scope,
                "size": size,
                "mtime": mtime,
            })
    return sorted(tree, key=lambda x: (x["folder"], x["name"]))


def save_upload(file_storage, scope, username, folder=""):
    filename = os.path.basename(file_storage.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise InvalidPathError(f"Dateityp nicht erlaubt: {ext or filename}")
    root = root_for(scope, username)
    folder = _safe(folder)
    target_dir = os.path.join(root, folder)
    os.makedirs(target_dir, exist_ok=True)
    dest = os.path.join(target_dir, filename)
    file_storage.save(dest)
    if ext in (".musicxml", ".mxl", ".xml"):
        dest = _convert_musicxml_to_midi(dest)
    return dest


def _convert_musicxml_to_midi(path):
    """MusicXML/.mxl -> .mid, played back like any other MIDI file."""
    try:
        from music21 import converter
        score = converter.parse(path)
        midi_path = os.path.splitext(path)[0] + ".mid"
        score.write("midi", fp=midi_path)
        return midi_path
    except Exception:
        return path


def resolve_path(scope, username, rel_path):
    root = root_for(scope, username)
    return os.path.join(root, _safe(rel_path))


def rmdir(scope, username, rel_path):
    import shutil as _sh
    p = os.path.join(root_for(scope, username), _safe(rel_path))
    if os.path.isdir(p):
        _sh.rmtree(p)


def display_title(full_path):
    """Song title from MIDI meta 'track_name'/'text', else filename with
    underscores -> spaces and no extension."""
    try:
        import mido
        mid = mido.MidiFile(full_path)
        for track in mid.tracks:
            for msg in track:
                if msg.is_meta and msg.type in ("track_name", "text") and msg.text.strip():
                    return msg.text.strip()
                if not msg.is_meta:
                    break
    except Exception:
        pass
    base = os.path.splitext(os.path.basename(full_path))[0]
    return base.replace("_", " ")


def inspect_channels(full_path, saved_channels=None, soundfont_path=None):
    """Which channels the file actually uses, their current program (from
    saved per-file overrides, else the file's own embedded program change --
    resolved against the actual SoundFont content if soundfont_path is
    given, so a bank the file asks for but the SoundFont doesn't have shows
    the register that will actually sound, not a nonexistent phantom one),
    a rough note count (used to prioritise which channels the UI shows first
    when a piece uses many voices), the average pitch (used by the organ
    page to guess which channel is the pedal line vs. a manual voice), and a
    voice_group: channels whose entire note timeline is identical (a very
    common pattern in organ MIDI exports -- the same voice duplicated onto
    two channels for a coupler/second registration) share the same
    voice_group id, so callers can treat them as one voice instead of two."""
    import mido
    saved_channels = saved_channels or {}
    programs = {}   # channel -> [bank, program]
    counts = {}
    pitch_sum = {}
    timelines = {}  # channel -> [(tick, note), ...], for duplicate-voice detection
    try:
        mid = mido.MidiFile(full_path)
        bank = {}
        tick = 0
        for msg in mido.merge_tracks(mid.tracks):
            tick += msg.time
            if msg.is_meta or not hasattr(msg, "channel"):
                continue
            ch = msg.channel
            if msg.type == "control_change" and msg.control == 0:
                bank[ch] = (bank.get(ch, 0) & 0x7F) | (msg.value << 7)
            elif msg.type == "control_change" and msg.control == 32:
                bank[ch] = (bank.get(ch, 0) & ~0x7F) | msg.value
            elif msg.type == "program_change":
                programs[ch] = [bank.get(ch, 0), msg.program]
            elif msg.type == "note_on" and msg.velocity > 0:
                counts[ch] = counts.get(ch, 0) + 1
                pitch_sum[ch] = pitch_sum.get(ch, 0) + msg.note
                programs.setdefault(ch, [bank.get(ch, 0), 0])
                timelines.setdefault(ch, []).append((tick, msg.note))
    except Exception:
        pass

    sig_to_leader = {}
    voice_group = {}
    for ch in sorted(timelines):
        sig = tuple(timelines[ch])
        voice_group[ch] = sig_to_leader.setdefault(sig, ch)

    channels = []
    for ch in sorted(counts.keys() or programs.keys()):
        override = saved_channels.get(str(ch), {})
        bank_v, program_v = programs.get(ch, [0, 0])
        has_override = "bank" in override or "program" in override
        if has_override:
            bank_v = int(override.get("bank", bank_v))
            program_v = int(override.get("program", program_v))
        elif soundfont_path:
            # No saved override -> this is the file's own (possibly
            # unmatched) bank/program; show what will actually be selected.
            bank_v, program_v = soundfont_info.resolve_preset(soundfont_path, bank_v, program_v)
        avg_note = round(pitch_sum.get(ch, 0) / counts[ch], 1) if counts.get(ch) else None
        channels.append({
            "channel": ch,
            "bank": bank_v,
            "program": program_v,
            "muted": bool(override.get("muted", False)),
            "avg_note": avg_note,
            "notes": counts.get(ch, 0),
            "voice_group": voice_group.get(ch, ch),
        })
    channels.sort(key=lambda c: -c["notes"])
    return channels


def mkdir(scope, username, rel_path):
    os.makedirs(os.path.join(root_for(scope, username), _safe(rel_path)), exist_ok=True)


def move(scope, username, src_rel, dst_rel):
    root = root_for(scope, username)
    src = os.path.join(root, _safe(src_rel))
    dst = os.path.join(root, _safe(dst_rel))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    for ext in (".json",):
        s, d = src + ext, dst + ext
        if os.path.exists(s):
            shutil.move(s, d)


def delete(scope, username, rel_path):
    p = os.path.join(root_for(scope, username), _safe(rel_path))
    if os.path.exists(p):
        os.remove(p)
    sidecar = p + ".json"
    if os.path.exists(sidecar):
        os.remove(sidecar)


# ---- per-file voice/channel settings sidecars --------------------------
def load_file_settings(full_path, soundfont):
    sidecar = full_path + ".json"
    if os.path.exists(sidecar):
        with open(sidecar) as f:
            data = json.load(f)
        if data.get("soundfont") == soundfont:
            return data.get("channels", {})
    return {}


def save_file_settings(full_path, soundfont, channels, as_copy_name=None):
    data = {"soundfont": soundfont, "channels": channels}
    if as_copy_name:
        sidecar = os.path.join(os.path.dirname(full_path), as_copy_name) + ".json"
    else:
        sidecar = full_path + ".json"
    with open(sidecar, "w") as f:
        json.dump(data, f, indent=2)
    return sidecar
