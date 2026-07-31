import os, socket, subprocess, functools, sqlite3, base64
from flask import (Flask, render_template, request, redirect, url_for, jsonify,
                    send_from_directory, flash)
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                          login_required, current_user)
from werkzeug.security import check_password_hash

import db, library, audio_devices, soundfont_info
from library import InvalidPathError
from gm_programs import GM_PROGRAM_NAMES
from synth_engine import engine

app = Flask(__name__)
app.secret_key = os.environ.get("VO_SECRET", "virtualorgan-static-secret-change-me")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


@app.errorhandler(InvalidPathError)
def _handle_invalid_path(err):
    if request.path.startswith(("/library/", "/upload", "/file/", "/play")):
        return jsonify({"ok": False, "error": str(err)}), 400
    flash(str(err))
    return redirect(url_for("index"))


@app.errorhandler(KeyError)
def _handle_missing_field(err):
    return jsonify({"ok": False, "error": f"Fehlendes Feld: {err}"}), 400


class User(UserMixin):
    def __init__(self, row):
        self.id = row["id"]; self.username = row["username"]; self.role = row["role"]

    def is_admin(self):
        return self.role == "admin"


@login_manager.user_loader
def load_user(uid):
    row = db.get_user_by_id(int(uid))
    return User(row) if row else None


def login_optional(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if db.get_setting("require_login") == "1" and not current_user.is_authenticated:
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if not (current_user.is_authenticated and current_user.is_admin()):
            flash("Nur für Administratoren.")
            return redirect(url_for("index"))
        return view(*a, **kw)
    return wrapped


def current_username():
    return current_user.username if current_user.is_authenticated else "gast"


# ---------------------------------------------------------------- auth ---
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        row = db.get_user(request.form["username"])
        if row and check_password_hash(row["password_hash"], request.form["password"]):
            login_user(User(row))
            return redirect(request.args.get("next") or url_for("index"))
        flash("Login fehlgeschlagen.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("index"))


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "favicon-32.png")


# ------------------------------------------------------------- library ---
@app.route("/")
@login_optional
def index():
    shared = library.list_tree("shared", current_username())
    personal = library.list_tree("personal", current_username()) if current_user.is_authenticated else []
    return render_template("index.html", shared=shared, personal=personal,
                            outputs=audio_devices.list_outputs(),
                            current_output=db.get_setting("output_device"))


@app.route("/organ")
@login_optional
def organ_page():
    return render_template("organ.html", inputs=audio_devices.list_midi_inputs(),
                            routes=db.get_midi_routes(),
                            outputs=audio_devices.list_outputs(),
                            current_output=db.get_setting("output_device"))


@app.route("/upload", methods=["POST"])
@login_optional
def upload():
    scope = request.form.get("scope", "shared")
    if scope == "personal" and not current_user.is_authenticated:
        scope = "shared"
    folder = request.form.get("folder", "")
    saved, rejected = 0, []
    for f in request.files.getlist("files"):
        if not f.filename:
            continue
        try:
            library.save_upload(f, scope, current_username(), folder)
            saved += 1
        except InvalidPathError:
            rejected.append(f.filename)
    if rejected:
        flash(f"Nicht unterstützter Dateityp, übersprungen: {', '.join(rejected)}")
    elif saved == 0:
        flash("Keine Datei ausgewählt.")
    return redirect(url_for("index"))


@app.route("/library/mkdir", methods=["POST"])
@login_optional
def mkdir():
    library.mkdir(request.form["scope"], current_username(), request.form["path"])
    return jsonify({"ok": True})


@app.route("/library/rmdir", methods=["POST"])
@login_optional
def rmdir():
    library.rmdir(request.form["scope"], current_username(), request.form["path"])
    return jsonify({"ok": True})


@app.route("/library/delete", methods=["POST"])
@login_optional
def delete_file():
    library.delete(request.form["scope"], current_username(), request.form["path"])
    return jsonify({"ok": True})


@app.route("/library/move", methods=["POST"])
@login_optional
def move_file():
    library.move(request.form["scope"], current_username(), request.form["src"], request.form["dst"])
    return jsonify({"ok": True})


# ------------------------------------------------------------- playback ---
@app.route("/play", methods=["POST"])
@login_optional
def play():
    d = request.get_json()
    output = d.get("output") or db.get_setting("output_device")
    path = library.resolve_path(d["scope"], current_username(), d["path"])
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "Datei nicht gefunden (evtl. gelöscht/verschoben)."}), 404
    soundfont = db.get_setting("soundfont")
    channels = d.get("channels") or library.load_file_settings(path, soundfont)
    default_profile = db.get_setting("default_profile")
    defaults = db.load_profile(default_profile) if default_profile else None
    title = library.display_title(path)
    engine.set_now_playing(title, library.inspect_channels(path, channels, soundfont))
    if output.startswith("web:"):
        token = engine.start_web_session(path, channels, soundfont, defaults=defaults)
        return jsonify({"mode": "web", "token": token, "title": title})
    try:
        engine.start(output, soundfont,
                     gain=float(db.get_setting("gain")),
                     reverb=db.get_setting("reverb") == "1",
                     chorus=db.get_setting("chorus") == "1")
    except Exception:
        engine.clear_now_playing()
        return jsonify({"ok": False,
                         "error": "Ausgabegerät nicht erreichbar. Ist es verbunden/eingeschaltet?"}), 503
    engine.play_file(path, channels, defaults=defaults)
    return jsonify({"mode": "realtime", "title": title})


@app.route("/stream/segment")
@login_optional
def stream_segment():
    token = request.args["token"]
    wav_bytes, events, done = engine.render_web_segment(token, seconds=8.0)
    if wav_bytes is None:
        return jsonify({"ok": False}), 410
    return jsonify({
        "ok": True,
        "audio": base64.b64encode(wav_bytes).decode("ascii"),
        "events": [{"t": t, "ch": ch, "note": note, "on": on} for (t, ch, note, on) in events],
        "done": done,
    })


@app.route("/stop", methods=["POST"])
@login_optional
def stop():
    d = request.get_json(silent=True) or {}
    if d.get("token"):
        engine.stop_web_session(d["token"])
    engine.stop_playback()
    engine.clear_now_playing()
    return jsonify({"ok": True})


@app.route("/pause", methods=["POST"])
@login_optional
def pause():
    # Only affects real-time (hardware) playback; web-browser preview is
    # paused entirely client-side (nothing is rendering server-side while
    # the browser isn't fetching segments), so this is a harmless no-op
    # in that case.
    engine.pause_playback()
    return jsonify({"ok": True})


@app.route("/resume", methods=["POST"])
@login_optional
def resume():
    engine.resume_playback()
    return jsonify({"ok": True})


@app.route("/status")
@login_optional
def status():
    return jsonify(engine.status())


# ------------------------------------------------ live note input (keyboard,
# browser MIDI, forwarded external devices routed through the browser) ------
@app.route("/keyboard/note", methods=["POST"])
@login_optional
def keyboard_note():
    d = request.get_json()
    try:
        engine.live_note(int(d["channel"]), int(d["note"]), int(d.get("velocity", 100)),
                          bool(d["on"]), db_get_setting=db.get_setting)
    except Exception:
        return jsonify({"ok": False, "error": "Ausgabegerät nicht erreichbar."}), 503
    return jsonify({"ok": True})


@app.route("/keyboard/program", methods=["POST"])
@login_optional
def keyboard_program():
    d = request.get_json()
    try:
        engine.ensure_started(db.get_setting)
    except Exception:
        return jsonify({"ok": False, "error": "Ausgabegerät nicht erreichbar."}), 503
    engine.set_channel_program(int(d["channel"]), int(d.get("bank", 0)), int(d["program"]))
    return jsonify({"ok": True})


@app.route("/keyboard/ensure_started", methods=["POST"])
@login_optional
def keyboard_ensure_started():
    try:
        engine.ensure_started(db.get_setting)
    except Exception:
        return jsonify({"ok": False, "error": "Ausgabegerät nicht erreichbar."}), 503
    return jsonify({"ok": True})


# ------------------------------------------------ per-file voice settings ---
@app.route("/file/channels")
@login_optional
def file_channels():
    scope, path = request.args["scope"], request.args["path"]
    full = library.resolve_path(scope, current_username(), path)
    soundfont = db.get_setting("soundfont")
    saved = library.load_file_settings(full, soundfont)
    return jsonify({
        "title": library.display_title(full),
        "channels": library.inspect_channels(full, saved, soundfont),
    })


@app.route("/file/settings", methods=["POST"])
@login_optional
def file_settings():
    d = request.get_json()
    full = library.resolve_path(d["scope"], current_username(), d["path"])
    soundfont = db.get_setting("soundfont")
    library.save_file_settings(full, soundfont, d["channels"], as_copy_name=d.get("save_as_copy_name"))
    return jsonify({"ok": True})


@app.route("/file/settings/reset", methods=["POST"])
@login_optional
def file_settings_reset():
    d = request.get_json()
    full = library.resolve_path(d["scope"], current_username(), d["path"])
    sidecar = full + ".json"
    if os.path.exists(sidecar):
        os.remove(sidecar)
    return jsonify({"ok": True})


@app.route("/soundfont/presets")
@login_optional
def soundfont_presets():
    presets = soundfont_info.list_presets(db.get_setting("soundfont"))
    grouped = {}
    for p in presets:
        grouped.setdefault(p["program"], []).append(p)
    out = []
    for program in sorted(grouped):
        label = GM_PROGRAM_NAMES[program] if program < len(GM_PROGRAM_NAMES) else f"Programm {program}"
        out.append({"program": program, "label": label, "presets": grouped[program]})
    return jsonify({"groups": out})


@app.route("/profiles", methods=["GET", "POST"])
@login_optional
def profiles():
    if request.method == "POST":
        d = request.get_json()
        db.save_profile(d["name"], d["channels"])
        if d.get("make_default"):
            db.set_setting("default_profile", d["name"])
        return jsonify({"ok": True})
    name = request.args.get("name")
    if name:
        return jsonify({"channels": db.load_profile(name) or {}})
    return jsonify({"profiles": db.list_profiles()})


# ------------------------------------------------------------------ midi ---
@app.route("/midi/routes", methods=["POST"])
@login_optional
def midi_routes():
    routes = request.get_json()["routes"]
    db.set_midi_routes(routes)
    engine.set_routes(routes)
    return jsonify({"ok": True})


# -------------------------------------------------------------- settings ---
SETTINGS_CHECKBOXES = ("require_login", "kiosk_enabled", "reverb", "chorus")
SETTINGS_FIELDS = ("output_device", "gain", "hostname")


@app.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings():
    if request.method == "POST":
        # HTML checkboxes are only present in the form data when checked, so
        # an unchecked box must explicitly be written as "0" here -- without
        # this, unchecking e.g. "Login erforderlich" or "Kiosk-Modus" would
        # silently have no effect and the old value would stick forever.
        for key in SETTINGS_CHECKBOXES:
            db.set_setting(key, "1" if request.form.get(key) == "1" else "0")
        for key in SETTINGS_FIELDS:
            if key in request.form and request.form[key] != "":
                db.set_setting(key, request.form[key])
        if request.form.get("hostname"):
            _apply_hostname(request.form["hostname"])
        _sync_kiosk_flag(db.get_setting("kiosk_enabled") == "1")
        flash("Einstellungen gespeichert.")
        return redirect(url_for("settings"))
    return render_template("settings.html", settings=db.all_settings(),
                            outputs=audio_devices.list_outputs(),
                            users=db.list_users(), hostname=socket.gethostname())


def _apply_hostname(name):
    # The webserver runs as the unprivileged 'virtualorgan' user, so this
    # goes through a narrowly-scoped sudoers NOPASSWD rule for exactly this
    # script (see /etc/sudoers.d/virtualorgan) rather than calling
    # hostnamectl directly, which would silently fail without it.
    try:
        subprocess.run(["sudo", "-n", "/usr/local/sbin/virtualorgan-set-hostname.sh", name], check=False)
    except Exception:
        pass


def _sync_kiosk_flag(enabled):
    """The kiosk systemd unit gates itself on the existence of a flag file
    (ConditionPathExists), completely independent of the settings DB. Keep
    the two in sync whenever the setting changes, and (re)start/stop the
    unit immediately so toggling it in the UI actually takes effect without
    requiring a manual reboot."""
    try:
        subprocess.run(["sudo", "-n", "/usr/local/sbin/virtualorgan-kiosk-ctl.sh",
                         "start" if enabled else "stop"], check=False)
    except Exception:
        pass


@app.route("/settings/users", methods=["POST"])
@login_required
@admin_required
def manage_users():
    action = request.form["action"]
    if action == "add":
        username = request.form["username"].strip()
        if not username or not request.form["password"]:
            flash("Benutzername und Passwort dürfen nicht leer sein.")
        else:
            try:
                db.add_user(username, request.form["password"], request.form.get("role", "user"))
            except sqlite3.IntegrityError:
                flash(f"Benutzername „{username}“ ist bereits vergeben.")
    elif action == "delete":
        uid = int(request.form["uid"])
        target = db.get_user_by_id(uid)
        if target and uid == current_user.id:
            flash("Der eigene Account kann nicht gelöscht werden.")
        elif target and target["role"] == "admin" and \
                sum(1 for u in db.list_users() if u["role"] == "admin") <= 1:
            flash("Der letzte Administrator kann nicht gelöscht werden.")
        else:
            db.delete_user(uid)
    elif action == "password":
        new_password = request.form.get("password", "")
        if len(new_password) < 4:
            flash("Das Passwort muss mindestens 4 Zeichen lang sein.")
        else:
            db.set_password(int(request.form["uid"]), new_password)
            flash("Passwort geändert.")
    return redirect(url_for("settings"))


if __name__ == "__main__":
    db.init_db()
    app.run(host="0.0.0.0", port=5000, threaded=True)
