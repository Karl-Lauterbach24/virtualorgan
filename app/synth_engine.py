"""Persistent FluidSynth engine shared by file playback, live external MIDI
controllers, the on-screen virtual keyboard, and browser-side Web MIDI input.

Web-browser output does NOT use a single infinite-length streamed WAV
(WebKit/iOS rejects that) -- instead each piece gets a WebSession that
renders successive short, fully-valid WAV segments on demand, resuming
exactly where the previous segment left off.
"""
import threading
import struct
import uuid as _uuid

import fluidsynth  # pyfluidsynth
import mido
import numpy as np

import soundfont_info

TAIL_SECONDS = 3.0   # let reverb/release ring out instead of cutting the last note
SAMPLE_RATE = 44100


def _wav_bytes(pcm: np.ndarray, sample_rate=SAMPLE_RATE, channels=2, bits=16):
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data = pcm.tobytes()
    header = (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits)
        + b"data" + struct.pack("<I", len(data))
    )
    return header + data


class ChanState:
    """Per-channel bank/program/mute tracking for one playback pass.
    `defaults` (a saved default profile) seeds a fallback register for
    channels the file itself never assigns one to; `overrides` (a per-file
    saved mapping) is stronger and locks the channel so the file's own
    program changes cannot override the user's explicit choice."""
    def __init__(self, overrides=None, defaults=None):
        self.bank, self.program, self.muted, self.locked = {}, {}, {}, set()
        for ch_str, cfg in (defaults or {}).items():
            ch = int(ch_str)
            self.bank[ch] = int(cfg.get("bank", 0))
            self.program[ch] = int(cfg.get("program", 0))
        for ch_str, cfg in (overrides or {}).items():
            ch = int(ch_str)
            self.bank[ch] = int(cfg.get("bank", 0))
            self.program[ch] = int(cfg.get("program", 0))
            self.muted[ch] = bool(cfg.get("muted", False))
            self.locked.add(ch)

    def is_muted(self, ch):
        return self.muted.get(ch, False)


def _select_program(s, sfid, soundfont_path, ch, bank, program):
    """program_select() with a fallback: many real-world organ MIDI files
    were authored against a different instrument's bank layout, so the
    bank-select value they send doesn't exist in this SoundFont even though
    the underlying program number is often still a perfectly valid,
    meaningful register in bank 0 (see soundfont_info.resolve_preset for the
    exact fallback chain). Without this, fluidsynth just silently keeps
    whatever preset was selected before -- which looks like "every voice is
    stuck on the same wrong instrument" to the user."""
    resolved_bank, resolved_program = soundfont_info.resolve_preset(soundfont_path, bank, program)
    s.program_select(ch, sfid, resolved_bank, resolved_program)
    return resolved_bank, resolved_program


def _feed_offline(s, msg, state, soundfont_path, resolved_state=None, events=None, t=0.0):
    """Apply one MIDI message to an offline (non-hardware) fluidsynth
    instance, honoring embedded program changes unless the channel is
    explicitly locked by the user's saved per-file settings.

    `resolved_state` (if given) is filled in with the actual, SoundFont-
    verified {channel: (bank, program)} as changes happen, so callers like
    WebSession can expose "what's really sounding" instead of the caller
    having to re-derive it. `events`/`t` (if given) collect a
    (time_offset_seconds, channel, note, is_on) tuple per note on/off, for
    client-side, sample-accurate key-press visualisation (see
    /stream/segment)."""
    ch = getattr(msg, "channel", None)
    if ch is None or state.is_muted(ch):
        return
    if msg.type == "control_change" and msg.control in (0, 32):
        state.bank[ch] = msg.value
    elif msg.type == "program_change":
        if ch not in state.locked:
            state.program[ch] = msg.program
        resolved = _select_program(s, s.sfid, soundfont_path, ch, state.bank.get(ch, 0), state.program.get(ch, 0))
        if resolved_state is not None:
            resolved_state[ch] = resolved
    elif msg.type == "note_on" and msg.velocity > 0:
        s.noteon(ch, msg.note, msg.velocity)
        if events is not None:
            events.append((t, ch, msg.note, True))
    elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
        s.noteoff(ch, msg.note)
        if events is not None:
            events.append((t, ch, msg.note, False))
    elif msg.type == "control_change":
        s.cc(ch, msg.control, msg.value)


class WebSession:
    """Holds one offline FluidSynth instance + MIDI iterator so successive
    /stream/segment requests can resume rendering exactly where the last
    one stopped, instead of re-rendering from the start each time."""
    def __init__(self, path, channel_settings, soundfont, defaults=None):
        self.synth = fluidsynth.Synth()
        self.synth.sfid = self.synth.sfload(soundfont)
        self.soundfont = soundfont
        self.state = ChanState(channel_settings, defaults)
        self.resolved_state = {}  # channel -> (bank, program), SoundFont-verified
        for ch in range(16):
            self.resolved_state[ch] = _select_program(
                self.synth, self.synth.sfid, soundfont, ch,
                self.state.bank.get(ch, 0), self.state.program.get(ch, 0))
        mid = mido.MidiFile(path)
        self.ticks_per_beat = mid.ticks_per_beat
        self.msg_iter = iter(mido.merge_tracks(mid.tracks))
        self.tempo = 500000
        self.finished = False
        self.tail_remaining = int(SAMPLE_RATE * TAIL_SECONDS)

    def render_seconds(self, seconds):
        target = int(SAMPLE_RATE * seconds)
        frames, got = [], 0
        events = []  # (offset_seconds_within_this_call, channel, note, is_on)
        while got < target and not (self.finished and self.tail_remaining <= 0):
            if self.finished:
                take = min(self.tail_remaining, 4096)
                frames.append(self.synth.get_samples(take))
                self.tail_remaining -= take
                got += take
                continue
            try:
                msg = next(self.msg_iter)
            except StopIteration:
                self.finished = True
                continue
            n = int(SAMPLE_RATE * mido.tick2second(msg.time, self.ticks_per_beat, self.tempo))
            if n > 0:
                frames.append(self.synth.get_samples(n))
                got += n
            if msg.type == "set_tempo":
                self.tempo = msg.tempo
            elif not msg.is_meta:
                _feed_offline(self.synth, msg, self.state, self.soundfont,
                               resolved_state=self.resolved_state, events=events, t=got / SAMPLE_RATE)
        data = np.concatenate(frames) if frames else np.zeros(2, dtype=np.int16)
        done = self.finished and self.tail_remaining <= 0
        return data, events, done

    def close(self):
        try:
            self.synth.delete()
        except Exception:
            pass


class SynthEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self.synth = None
        self.sfid = None
        self.soundfont = None
        self.driver = None
        self.playing = False
        self.paused = False
        self._play_thread = None
        self._stop_flag = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # set = running, cleared = paused
        self.midi_inputs = {}
        self.routes = []
        self.active_notes = {ch: set() for ch in range(16)}
        self.channel_state = {ch: {"bank": 0, "program": 0} for ch in range(16)}
        self.state_lock = threading.Lock()
        self.web_sessions = {}
        self.now_playing = None

    # ---- lifecycle -----------------------------------------------------
    def start(self, output_id, soundfont, gain=0.6, reverb=True, chorus=False):
        with self.lock:
            self._teardown_synth()
            driver, device = self._resolve_output(output_id)
            s = fluidsynth.Synth(gain=gain)
            kwargs = {"device": device} if device else {}
            s.start(driver=driver, **kwargs)
            sfid = s.sfload(soundfont)
            for ch in range(16):
                s.program_select(ch, sfid, 0, 0)
            if reverb:
                try:
                    s.set_reverb(0.6, 0.4, 0.6, 0.4)
                except Exception:
                    pass
            if chorus:
                mod = getattr(fluidsynth, "FLUID_CHORUS_MOD_SINE", 0)
                try:
                    s.set_chorus(3, 1.2, 0.3, 8.0, mod)
                except Exception:
                    pass
            self.synth, self.sfid, self.soundfont, self.driver = s, sfid, soundfont, driver
            self.active_notes = {ch: set() for ch in range(16)}
            self.channel_state = {ch: {"bank": 0, "program": 0} for ch in range(16)}

    def ensure_started(self, db_get_setting):
        if self.synth is None:
            self.start(db_get_setting("output_device"), db_get_setting("soundfont"),
                       gain=float(db_get_setting("gain")),
                       reverb=db_get_setting("reverb") == "1",
                       chorus=db_get_setting("chorus") == "1")

    def _teardown_synth(self):
        self.stop_playback()
        if self.synth is not None:
            try:
                self.synth.delete()
            except Exception:
                pass
            self.synth = None

    @staticmethod
    def _resolve_output(output_id):
        if output_id.startswith("alsa:"):
            return "alsa", output_id.split(":", 1)[1]
        if output_id.startswith("bluetooth:"):
            return "alsa", f"bluealsa:DEV={output_id.split(':', 1)[1]},PROFILE=a2dp"
        if output_id.startswith("network:"):
            return "pulseaudio", output_id.split(":", 1)[1]
        return "alsa", None

    # ---- status for the visualizer --------------------------------------
    def status(self):
        with self.state_lock:
            channel_state = {str(ch): st for ch, st in self.channel_state.items()}
            web_active = bool(self.web_sessions)
            if web_active:
                # Real-time playback's own channel_state doesn't apply while a
                # web-preview session is active (that session runs its own
                # offline synth) -- without this, the visualizer/manuals were
                # stuck showing whatever program_select(0,0) resolves to
                # ("Montre 8") for every channel during web-preview playback,
                # regardless of what the file actually assigned.
                sess = next(iter(self.web_sessions.values()))
                channel_state = {str(ch): {"bank": b, "program": p}
                                  for ch, (b, p) in sess.resolved_state.items()}
            return {
                "playing": self.playing or web_active,
                "paused": self.paused,
                "web_preview": web_active,
                "active_notes": {str(ch): sorted(n) for ch, n in self.active_notes.items() if n},
                "channel_state": channel_state,
                "now_playing": self.now_playing,
            }

    def set_now_playing(self, title, channels):
        self.now_playing = {"title": title, "channels": channels}

    def clear_now_playing(self):
        self.now_playing = None

    # ---- file playback through hardware output ------------------------------
    def play_file(self, path, channel_settings=None, defaults=None):
        self.stop_playback()
        self._stop_flag.clear()
        self._pause_event.set()
        self.paused = False
        state = ChanState(channel_settings, defaults)
        self._play_thread = threading.Thread(target=self._play_realtime, args=(path, state), daemon=True)
        self._play_thread.start()
        self.playing = True

    def _play_realtime(self, path, state):
        try:
            for msg in mido.MidiFile(path).play():
                if self._stop_flag.is_set():
                    break
                self._pause_event.wait()  # blocks here while paused; mido's own
                                           # play() pacing simply resumes counting
                                           # from the moment we call next() again
                if self._stop_flag.is_set():
                    break
                self._feed(msg, state)
        finally:
            self.playing = False

    def pause_playback(self):
        """Only meaningful for real-time (hardware) playback -- web-browser
        preview pauses purely client-side (see app.js), nothing server-side
        is rendering while the client isn't fetching segments."""
        if not self.playing or self.paused:
            return
        self._pause_event.clear()
        self.paused = True
        if self.synth:
            with self.state_lock:
                notes = {ch: list(n) for ch, n in self.active_notes.items() if n}
            for ch, notes_on in notes.items():
                for note in notes_on:
                    self.synth.noteoff(ch, note)

    def resume_playback(self):
        if not self.playing or not self.paused:
            return
        self.paused = False
        self._pause_event.set()

    def stop_playback(self):
        self._stop_flag.set()
        self._pause_event.set()  # release anything blocked in pause so it can see the stop flag
        if self._play_thread and self._play_thread.is_alive():
            self._play_thread.join(timeout=2)
        self.playing = False
        self.paused = False
        with self.state_lock:
            for ch in self.active_notes:
                self.active_notes[ch] = set()

    # ---- web-browser output: segment sessions --------------------------------
    def start_web_session(self, path, channel_settings, soundfont, defaults=None):
        for sess in self.web_sessions.values():
            sess.close()
        self.web_sessions.clear()
        token = _uuid.uuid4().hex
        self.web_sessions[token] = WebSession(path, channel_settings, soundfont, defaults)
        return token

    def render_web_segment(self, token, seconds=8.0):
        sess = self.web_sessions.get(token)
        if not sess:
            return None, [], True
        pcm, events, done = sess.render_seconds(seconds)
        if done:
            sess.close()
            del self.web_sessions[token]
        return _wav_bytes(pcm), events, done

    def stop_web_session(self, token):
        sess = self.web_sessions.pop(token, None)
        if sess:
            sess.close()

    # ---- live note input: virtual keyboard, browser MIDI, external devices -
    def live_note(self, channel, note, velocity, on, db_get_setting=None):
        if self.synth is None and db_get_setting:
            self.ensure_started(db_get_setting)
        if not self.synth:
            return
        if on and velocity > 0:
            self.synth.noteon(channel, note, velocity)
            with self.state_lock:
                self.active_notes.setdefault(channel, set()).add(note)
        else:
            self.synth.noteoff(channel, note)
            with self.state_lock:
                self.active_notes.setdefault(channel, set()).discard(note)

    def set_channel_program(self, channel, bank, program):
        if not self.synth:
            return
        resolved_bank, resolved_program = _select_program(
            self.synth, self.sfid, self.soundfont, channel, bank, program)
        with self.state_lock:
            self.channel_state[channel] = {"bank": resolved_bank, "program": resolved_program}

    def _feed(self, msg, state=None):
        if not self.synth or msg.is_meta:
            return
        ch = getattr(msg, "channel", None)
        if ch is None:
            return
        if state and state.is_muted(ch):
            return
        if state and msg.type == "control_change" and msg.control in (0, 32):
            state.bank[ch] = msg.value
            return
        if msg.type == "program_change":
            bank = 0
            if state:
                if ch not in state.locked:
                    state.program[ch] = msg.program
                bank = state.bank.get(ch, 0)
                program = state.program.get(ch, 0)
            else:
                program = msg.program
            resolved_bank, resolved_program = _select_program(self.synth, self.sfid, self.soundfont, ch, bank, program)
            with self.state_lock:
                self.channel_state[ch] = {"bank": resolved_bank, "program": resolved_program}
            return
        if msg.type == "note_on" and msg.velocity > 0:
            self.synth.noteon(ch, msg.note, msg.velocity)
            with self.state_lock:
                self.active_notes.setdefault(ch, set()).add(msg.note)
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            self.synth.noteoff(ch, msg.note)
            with self.state_lock:
                self.active_notes.setdefault(ch, set()).discard(msg.note)
        elif msg.type == "control_change":
            self.synth.cc(ch, msg.control, msg.value)
        elif msg.type == "pitchwheel":
            self.synth.pitch_bend(ch, msg.pitch)

    # ---- external physical MIDI controllers ---------------------------------
    def set_routes(self, routes):
        """routes: [{port_name, mode:'single'|'multi', channel_in:-1|n, channel_out:n}]"""
        with self.lock:
            for port in self.midi_inputs.values():
                port.close()
            self.midi_inputs.clear()
            self.routes = routes
            for name in {r["port_name"] for r in routes}:
                try:
                    self.midi_inputs[name] = mido.open_input(name, callback=self._make_callback(name))
                except Exception:
                    pass

    def _make_callback(self, port_name):
        def cb(msg):
            if not self.synth or msg.is_meta or not hasattr(msg, "channel"):
                return
            for r in self.routes:
                if r["port_name"] != port_name:
                    continue
                if r.get("mode", "single") == "multi":
                    self._feed(msg)
                    continue
                if r["channel_in"] != -1 and msg.channel != r["channel_in"]:
                    continue
                self._feed(msg.copy(channel=r["channel_out"]))
        return cb


engine = SynthEngine()
