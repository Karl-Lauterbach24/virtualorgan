const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

// ---- Toasts: surface errors instead of failing silently -------------------
function toastHost() {
  let host = $("#toastHost");
  if (!host) {
    host = document.createElement("div");
    host.id = "toastHost";
    document.body.appendChild(host);
  }
  return host;
}
function toast(message, isError = false) {
  const now = Date.now();
  if (isError && toast._last === message && now - toast._lastAt < 4000) return;
  toast._last = message; toast._lastAt = now;
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = message;
  toastHost().appendChild(el);
  setTimeout(() => el.remove(), isError ? 5000 : 3000);
}

// Wraps fetch with a friendly error toast on network failure or a non-2xx
// JSON {ok:false, error:"..."} response, so failed actions (deleted file,
// unreachable output device, invalid folder name, ...) are visible instead
// of just doing nothing.
async function api(url, options) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (e) {
    toast("Keine Verbindung zum Server.", true);
    throw e;
  }
  if (!res.ok) {
    let msg = `Fehler (${res.status}).`;
    try {
      const data = await res.clone().json();
      if (data && data.error) msg = data.error;
    } catch (e) { /* not JSON, keep generic message */ }
    toast(msg, true);
  }
  return res;
}

function outputId() {
  const sel = $("#outputSelect");
  return sel ? sel.value : "web:browser";
}

// ---- Register/Presets (shared by file-settings modal + organ manuals) ----
let presetsIndex = null;
async function loadPresets() {
  if (presetsIndex) return presetsIndex;
  try {
    const data = await (await fetch("/soundfont/presets")).json();
    presetsIndex = data.groups;
  } catch (e) {
    presetsIndex = [];
  }
  return presetsIndex;
}
function presetName(bank, program) {
  if (!presetsIndex) return `Programm ${program}`;
  const grp = presetsIndex.find((g) => g.program === program);
  if (!grp) return `Programm ${program}`;
  const p = grp.presets.find((x) => x.bank === bank) || grp.presets[0];
  return p ? p.name : `Programm ${program}`;
}
function buildGroupedSelect(selectEl, selectedBank, selectedProgram) {
  selectEl.innerHTML = "";
  (presetsIndex || []).forEach((g) => {
    const og = document.createElement("optgroup");
    og.label = g.label;
    g.presets.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = `${p.bank},${g.program}`;
      opt.textContent = `000 ${String(p.bank).padStart(3, "0")} ${String(g.program).padStart(3, "0")} ${p.name}`;
      if (p.bank === selectedBank && g.program === selectedProgram) opt.selected = true;
      og.appendChild(opt);
    });
    selectEl.appendChild(og);
  });
}

// ---- Visualizer (shared: index page shows the clicked file's voices, organ
// page shows the live manuals + anything else currently sounding) -----------
let manualsFallback = null;   // organ page: shown when nothing is playing
let localOverride = null;     // instant client-side feedback right after clicking play
let lastStatus = {};          // last /status payload (realtime mode reads active_notes from here)

function mapChannels(list) {
  return (list || []).map((c) => ({
    channel: String(c.channel), label: `Kanal ${c.channel + 1}`,
    avg_note: c.avg_note, notes: c.notes,
    voice_group: String(c.voice_group != null ? c.voice_group : c.channel),
  }));
}

async function renderVisualizerTick() {
  let status = {};
  try { status = await (await fetch("/status")).json(); } catch (e) { /* ignore */ }
  lastStatus = status;
  const chstate = status.channel_state || {};
  const titleEl = $("#nowPlayingTitle");

  let channels = [], title = null, filePlaying = false;
  if (status.now_playing) {
    channels = mapChannels(status.now_playing.channels);
    title = status.now_playing.title;
    filePlaying = true;
    localOverride = null;
  } else if (localOverride) {
    channels = localOverride.channels; title = localOverride.title; filePlaying = true;
  } else if (manualsFallback) {
    channels = manualsFallback;
  }
  if (titleEl) titleEl.textContent = title || "Kein Stück ausgewählt";

  // If real-time (hardware) playback is running, our pause button must
  // reflect the server's actual paused state -- e.g. after switching pages
  // the button needs to come back showing the right icon, not always "⏸".
  if (currentMode === "realtime" && status.playing) {
    if (isPaused !== !!status.paused) { isPaused = !!status.paused; updatePlayPauseIcon(); }
    setTransportEnabled(true);
  } else if (currentMode === "realtime" && !status.playing) {
    // playback ended/was stopped elsewhere (e.g. another tab, or it simply finished)
    currentMode = null; isPaused = false; setTransportEnabled(false); updatePlayPauseIcon();
  }

  const active = computeActiveNotes();

  const el = $("#visualizer");
  if (el) {
    el.innerHTML = "";
    if (!channels.length) {
      el.innerHTML = '<div class="hint">Keine aktiven Stimmen.</div>';
    } else {
      channels.forEach((c) => {
        const st = chstate[c.channel] || { bank: 0, program: 0 };
        const name = presetName(st.bank, st.program);
        const lit = !!(active[c.channel] && active[c.channel].size > 0);
        const row = document.createElement("div");
        row.className = "vis-row" + (lit ? " lit" : "");
        row.innerHTML = `<span class="vis-dot"></span><span class="vis-label">${c.label}</span><span class="vis-reg">${name}</span>`;
        el.appendChild(row);
      });
    }
  }

  updateManualsFromPlayback(channels, chstate, active, filePlaying);
}
setInterval(renderVisualizerTick, 400);
loadPresets();

// ---- Web-browser playback: gapless via Web Audio API ----------------------
// The server still streams successive short WAV segments (WebKit/iOS reject
// one huge streamed WAV), but instead of feeding each blob into a fresh
// <audio>.src (which forces a reload/decode step and produces an audible
// click/gap at every segment boundary), each segment is decoded into an
// AudioBuffer and scheduled back-to-back on the AudioContext's own sample
// clock: nextStartTime += buffer.duration. That's the standard gapless
// Web-Audio scheduling pattern -- no gap, independent of network/decode
// jitter as long as we stay far enough ahead (see the 1-segment lookahead
// below).
//
// Each segment also carries a note on/off event log (server-rendered, so it
// reflects exactly what will sound). Because segments are fetched one
// ahead of playback, "the server just processed this note" and "the
// listener actually hears it" are up to ~8s apart -- so events are stamped
// with the AudioContext time they'll actually play (segment startAt + event
// offset) and replayed against audioCtx.currentTime, not against when the
// fetch happened. That's what keeps the manuals' key highlighting in sync
// with what's audible instead of running ahead of it.
let audioCtx = null;
let currentWebToken = null;
let webPlaybackActive = false;
let nextStartTime = 0;
let scheduledSources = [];
let webEventTimeline = [];   // [{atTime, ch, note, on}, ...] sorted ascending by atTime
let webEventCursor = 0;
let webActiveNotes = {};     // channel(string) -> Set<note> currently sounding (web-preview mode)

let currentMode = null;     // 'web' | 'realtime' | null
let isPaused = false;

function getAudioCtx() {
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  return audioCtx;
}

function base64ToArrayBuffer(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

async function fetchAndDecodeSegment(token) {
  const res = await fetch(`/stream/segment?token=${token}&_=${Date.now()}`);
  if (res.status === 410) return null;
  const payload = await res.json();
  if (!payload.ok) return null;
  const audioBuffer = await getAudioCtx().decodeAudioData(base64ToArrayBuffer(payload.audio));
  return { audioBuffer, events: payload.events, done: payload.done };
}

// Advances the read cursor over webEventTimeline up to `now` (an
// audioCtx.currentTime value), replaying note on/off into webActiveNotes.
// O(1) amortized: each event is only ever visited once as time passes.
function advanceWebEventCursor(now) {
  while (webEventCursor < webEventTimeline.length && webEventTimeline[webEventCursor].atTime <= now) {
    const e = webEventTimeline[webEventCursor];
    const key = String(e.ch);
    if (!webActiveNotes[key]) webActiveNotes[key] = new Set();
    if (e.on) webActiveNotes[key].add(e.note);
    else webActiveNotes[key].delete(e.note);
    webEventCursor++;
  }
  if (webEventCursor > 1000) {  // bound memory on long pieces
    webEventTimeline = webEventTimeline.slice(webEventCursor);
    webEventCursor = 0;
  }
}

// Mode-aware "what's sounding right now, per channel" -- used by both the
// text voice list and the manuals' key highlighting so they never disagree.
function computeActiveNotes() {
  if (currentMode === "web") {
    advanceWebEventCursor(getAudioCtx().currentTime);
    return webActiveNotes;
  }
  const out = {};
  Object.entries(lastStatus.active_notes || {}).forEach(([ch, notes]) => { out[ch] = new Set(notes); });
  return out;
}

async function runWebPlayback(token) {
  webPlaybackActive = true;
  nextStartTime = 0;
  let pending = fetchAndDecodeSegment(token);
  while (webPlaybackActive && currentWebToken === token) {
    let seg;
    try {
      seg = await pending;
    } catch (e) {
      toast("Wiedergabe unterbrochen (Segment konnte nicht dekodiert werden).", true);
      break;
    }
    if (!seg || currentWebToken !== token) break;
    // Kick off fetching+decoding the NEXT segment immediately, so that
    // network/decode latency overlaps with this segment's ~8s of playback
    // instead of creating a gap when we get to it.
    pending = seg.done ? null : fetchAndDecodeSegment(token);
    const ctx = getAudioCtx();
    const startAt = Math.max(nextStartTime, ctx.currentTime + 0.06);
    const src = ctx.createBufferSource();
    src.buffer = seg.audioBuffer;
    src.connect(ctx.destination);
    src.start(startAt);
    src.onended = () => { scheduledSources = scheduledSources.filter((x) => x !== src); };
    scheduledSources.push(src);
    (seg.events || []).forEach((e) => webEventTimeline.push({ atTime: startAt + e.t, ch: e.ch, note: e.note, on: e.on }));
    nextStartTime = startAt + seg.audioBuffer.duration;
    if (seg.done) { webPlaybackActive = false; break; }
    if (!pending) break;
  }
}

function stopWebPlayback() {
  webPlaybackActive = false;
  scheduledSources.forEach((s) => { try { s.stop(); } catch (e) { /* already ended */ } });
  scheduledSources = [];
  nextStartTime = 0;
  webEventTimeline = [];
  webEventCursor = 0;
  webActiveNotes = {};
}

// ---- Transport bar: shared Play/Pause/Stop + title, lives in base.html so
// it (and playback state above) survives switching between Bibliothek and
// Virtuelle Orgel via the soft-navigation router further down. ------------
function setTransportEnabled(enabled) {
  const pp = $("#transportPlayPause"), stop = $("#stopBtn");
  if (pp) pp.disabled = !enabled;
  if (stop) stop.disabled = !enabled;
}
function updatePlayPauseIcon() {
  const pp = $("#transportPlayPause");
  if (pp) pp.textContent = isPaused ? "▶" : "⏸";
}

async function togglePlayPause() {
  if (currentMode === "web") {
    const ctx = getAudioCtx();
    if (isPaused) { await ctx.resume(); isPaused = false; }
    else { await ctx.suspend(); isPaused = true; }
    updatePlayPauseIcon();
  } else if (currentMode === "realtime") {
    const res = await api(isPaused ? "/resume" : "/pause", { method: "POST" });
    if (res.ok) { isPaused = !isPaused; updatePlayPauseIcon(); }
  }
}

async function playFile(scope, path) {
  // Touch the AudioContext synchronously-ish (before the first await) so a
  // browser's autoplay policy still recognises this as tied to the click.
  getAudioCtx().resume().catch(() => {});
  const chRes = await api(`/file/channels?scope=${scope}&path=${encodeURIComponent(path)}`);
  if (!chRes.ok) return;
  const chData = await chRes.json();
  localOverride = { title: chData.title, channels: mapChannels(chData.channels) };
  manualSlots = [null, null, null];
  renderVisualizerTick();

  const output = outputId();
  const res = await api("/play", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope, path, output })
  });
  if (!res.ok) { localOverride = null; renderVisualizerTick(); return; }
  const data = await res.json();
  stopWebPlayback();
  currentMode = data.mode;
  isPaused = false;
  setTransportEnabled(true);
  updatePlayPauseIcon();
  if (data.mode === "web") {
    currentWebToken = data.token;
    runWebPlayback(currentWebToken);
  }
}

async function stopPlayback() {
  stopWebPlayback();
  const token = currentWebToken;
  currentWebToken = null;
  currentMode = null;
  isPaused = false;
  localOverride = null;
  manualSlots = [null, null, null];
  setTransportEnabled(false);
  updatePlayPauseIcon();
  await api("/stop", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token })
  });
  renderVisualizerTick();
}

// ---- File-settings modal (Stimmen einer Datei neu der SF zuordnen) --------
async function openVoiceSettings(scope, path) {
  await loadPresets();
  const res = await api(`/file/channels?scope=${scope}&path=${encodeURIComponent(path)}`);
  if (!res.ok) return;
  const data = await res.json();
  const modal = document.createElement("div");
  modal.className = "modal-backdrop";
  modal.innerHTML = `<div class="modal">
    <h3>${data.title}</h3>
    <div class="modal-channels"></div>
    <div class="modal-actions">
      <button id="modalReset">Auf Datei-Standard zurücksetzen</button>
      <button id="modalSaveDefault">Als globalen Standard speichern</button>
      <button id="modalSaveCopy">Als Kopie speichern</button>
      <button id="modalSave">Speichern</button>
      <button id="modalCancel">Abbrechen</button>
    </div>
  </div>`;
  document.body.appendChild(modal);
  const container = modal.querySelector(".modal-channels");
  if (!data.channels.length) {
    container.innerHTML = '<div class="hint">Keine Noten-Kanäle in dieser Datei gefunden.</div>';
  }
  data.channels.forEach((c) => {
    const row = document.createElement("div");
    row.className = "modal-channel-row";
    row.dataset.channel = c.channel;
    const label = document.createElement("span");
    label.textContent = `Kanal ${c.channel + 1} (${c.notes} Noten)`;
    const sel = document.createElement("select");
    buildGroupedSelect(sel, c.bank, c.program);
    const muteLabel = document.createElement("label");
    muteLabel.className = "mute-label";
    const mute = document.createElement("input");
    mute.type = "checkbox";
    mute.checked = c.muted;
    mute.className = "mute-check";
    muteLabel.appendChild(mute);
    muteLabel.append(" Stumm");
    row.append(label, sel, muteLabel);
    container.appendChild(row);
  });
  modal.querySelector("#modalCancel").onclick = () => modal.remove();
  modal.querySelector("#modalReset").onclick = async () => {
    if (!confirm("Gespeicherte Zuordnung für diese Datei löschen und Datei-Standard verwenden?")) return;
    const res = await api("/file/settings/reset", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope, path })
    });
    if (res.ok) { toast("Zurückgesetzt."); modal.remove(); }
  };
  modal.querySelector("#modalSaveDefault").onclick = async () => {
    const channels = {};
    container.querySelectorAll(".modal-channel-row").forEach((row) => {
      const ch = row.dataset.channel;
      const [bank, program] = row.querySelector("select").value.split(",").map(Number);
      channels[ch] = { bank, program, muted: row.querySelector(".mute-check").checked };
    });
    const res = await api("/profiles", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "_default", channels, make_default: true })
    });
    if (res.ok) toast("Als globaler Standard für Dateien ohne eigene Registrierung gespeichert.");
  };
  const doSave = async (copyName) => {
    const channels = {};
    container.querySelectorAll(".modal-channel-row").forEach((row) => {
      const ch = row.dataset.channel;
      const [bank, program] = row.querySelector("select").value.split(",").map(Number);
      const muted = row.querySelector(".mute-check").checked;
      channels[ch] = { bank, program, muted };
    });
    const res = await api("/file/settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope, path, channels, save_as_copy_name: copyName })
    });
    if (res.ok) { toast("Gespeichert."); modal.remove(); }
  };
  modal.querySelector("#modalSave").onclick = () => doSave(null);
  modal.querySelector("#modalSaveCopy").onclick = () => {
    const name = prompt("Dateiname für die Kopie der Einstellungen:");
    if (name) doSave(name);
  };
}

// ---- global click delegation: library actions + MIDI routes ---------------
document.addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-path]");
  if (e.target.matches(".btn-play") && row) {
    if (e.target.classList.contains("busy")) return;
    e.target.classList.add("busy");
    playFile(row.dataset.scope, row.dataset.path).finally(() => e.target.classList.remove("busy"));
  }
  if (e.target.matches(".btn-settings") && row) openVoiceSettings(row.dataset.scope, row.dataset.path);
  if (e.target.matches(".btn-delete") && row) {
    if (confirm("Datei wirklich löschen?")) {
      api("/library/delete", {
        method: "POST", body: new URLSearchParams({ scope: row.dataset.scope, path: row.dataset.path })
      }).then((res) => { if (res.ok) location.reload(); });
    }
  }
  if (e.target.matches(".btn-move") && row) {
    const dest = prompt("Zielordner (leer = Hauptordner):", row.dataset.folder || "");
    if (dest === null) return;
    const filename = row.dataset.path.split("/").pop();
    const dst = dest ? `${dest}/${filename}` : filename;
    api("/library/move", {
      method: "POST", body: new URLSearchParams({ scope: row.dataset.scope, src: row.dataset.path, dst })
    }).then((res) => { if (res.ok) location.reload(); });
  }
  if (e.target.id === "stopBtn") stopPlayback();
  if (e.target.id === "transportPlayPause") togglePlayPause();

  if (e.target.matches(".btn-delete-route")) {
    const tr = e.target.closest("tr");
    const routes = $$("#routeTable tbody tr").filter((r) => r !== tr && r.children.length > 1).map(routeFromRow);
    api("/midi/routes", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ routes })
    }).then((res) => { if (res.ok) location.reload(); });
  }
});

function routeFromRow(tr) {
  return {
    port_name: tr.children[0].textContent,
    mode: tr.children[1].textContent.includes("Mehrere") ? "multi" : "single",
    channel_in: tr.children[2].textContent === "Alle" ? -1 : parseInt(tr.children[2].textContent) - 1,
    channel_out: tr.children[3].textContent === "–" ? 0 : parseInt(tr.children[3].textContent) - 1,
  };
}

function initAddRouteButton() {
  const btn = $("#addRoute");
  if (!btn || btn.dataset.bound) return;
  btn.dataset.bound = "1";
  btn.addEventListener("click", async () => {
    const portName = $("#portSelect").value;
    if (!portName) { toast("Kein MIDI-Gerät verfügbar/ausgewählt.", true); return; }
    const routes = $$("#routeTable tbody tr").filter((r) => r.children.length > 1).map(routeFromRow);
    routes.push({
      port_name: portName,
      mode: $("#routeMode").value,
      channel_in: $("#chIn").value ? parseInt($("#chIn").value) - 1 : -1,
      channel_out: parseInt($("#chOut").value || "1") - 1,
    });
    const res = await api("/midi/routes", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ routes })
    });
    if (res.ok) location.reload();
  });
}

// ---- Bibliothek: echte Ordner-Navigation, Mehrfachauswahl, Umbenennen,
// Drag&Drop-Upload, Sortierung -- mehr wie ein richtiger Dateimanager. -----
const PAGE_SIZE = 20;

function formatSize(bytes) {
  if (!bytes) return "–";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
function formatDate(ts) {
  if (!ts) return "–";
  return new Date(ts * 1000).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" });
}

const LIB_SORTERS = {
  name: (a, b) => a.name.localeCompare(b.name),
  date: (a, b) => b.mtime - a.mtime,
  size: (a, b) => b.size - a.size,
};

function initLibBrowser(el) {
  const scope = el.dataset.scope;
  const files = JSON.parse(el.dataset.files || "[]");
  const state = { path: [], search: "", page: 1, sort: "name", selected: new Set() };

  const currentFolder = () => state.path.join("/");

  function childFolders() {
    const cur = currentFolder();
    const prefix = cur ? cur + "/" : "";
    const names = new Set();
    files.forEach((f) => {
      const folder = f.folder || "";
      if (cur) {
        if (folder !== cur && !folder.startsWith(prefix)) return;
        const rest = folder.slice(prefix.length);
        if (rest) names.add(rest.split("/")[0]);
      } else if (folder) {
        names.add(folder.split("/")[0]);
      }
    });
    return [...names].sort((a, b) => a.localeCompare(b));
  }

  function folderFileCount(name) {
    const full = currentFolder() ? `${currentFolder()}/${name}` : name;
    return files.filter((f) => (f.folder || "") === full || (f.folder || "").startsWith(full + "/")).length;
  }

  function filesInCurrentFolder() {
    return files.filter((f) => (f.folder || "") === currentFolder());
  }

  function visibleFiles() {
    let list;
    if (state.search) {
      const q = state.search.toLowerCase();
      list = files.filter((f) => f.name.toLowerCase().includes(q));
    } else {
      list = filesInCurrentFolder();
    }
    return [...list].sort(LIB_SORTERS[state.sort] || LIB_SORTERS.name);
  }

  function renderBreadcrumb(container) {
    const bc = document.createElement("div");
    bc.className = "breadcrumb";
    const home = document.createElement("span");
    home.className = "breadcrumb-item" + (state.path.length === 0 ? " current" : "");
    home.textContent = "🏠 Hauptordner";
    home.addEventListener("click", () => { state.path = []; state.page = 1; render(); });
    bc.appendChild(home);
    state.path.forEach((seg, i) => {
      const sep = document.createElement("span");
      sep.className = "breadcrumb-sep"; sep.textContent = "›";
      bc.appendChild(sep);
      const item = document.createElement("span");
      item.className = "breadcrumb-item" + (i === state.path.length - 1 ? " current" : "");
      item.textContent = seg;
      item.addEventListener("click", () => { state.path = state.path.slice(0, i + 1); state.page = 1; render(); });
      bc.appendChild(item);
    });
    container.appendChild(bc);
  }

  function fileRowHtml(f) {
    const showFolder = !!state.search && f.folder;
    return `<tr data-scope="${f.scope}" data-path="${f.path}" data-folder="${f.folder || ""}" data-name="${f.name}">
      <td class="col-check"><input type="checkbox" class="row-check"></td>
      <td class="col-name">🎵 ${f.name}${showFolder ? `<div class="file-subfolder">📁 ${f.folder}</div>` : ""}</td>
      <td class="col-meta">${formatSize(f.size)}</td>
      <td class="col-meta">${formatDate(f.mtime)}</td>
      <td class="actions">
        <button class="btn-play" title="Abspielen">▶</button>
        <button class="btn-settings" title="Stimmeneinstellungen">⚙</button>
        <button class="btn-rename" title="Umbenennen">✏️</button>
        <button class="btn-move" title="Verschieben">📁➡</button>
        <button class="btn-delete" title="Löschen">🗑</button>
      </td></tr>`;
  }

  async function uploadFiles(fileList) {
    if (!fileList.length) return;
    const fd = new FormData();
    fd.append("scope", scope);
    fd.append("folder", currentFolder());
    [...fileList].forEach((f) => fd.append("files", f));
    toast(`Lade ${fileList.length} Datei(en) hoch…`);
    const res = await api("/upload", { method: "POST", body: fd });
    if (res.ok) location.reload();
  }

  function bindDragDrop(container) {
    let depth = 0;
    container.addEventListener("dragenter", (e) => {
      e.preventDefault(); depth++; container.classList.add("drag-over");
    });
    container.addEventListener("dragover", (e) => e.preventDefault());
    container.addEventListener("dragleave", () => { depth--; if (depth <= 0) container.classList.remove("drag-over"); });
    container.addEventListener("drop", (e) => {
      e.preventDefault(); depth = 0; container.classList.remove("drag-over");
      if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
    });
  }

  function render() {
    el.innerHTML = "";

    const toolbar = document.createElement("div");
    toolbar.className = "lib-toolbar";
    const search = document.createElement("input");
    search.type = "text";
    search.placeholder = "🔍 Suche über alle Ordner…";
    search.value = state.search;
    search.className = "lib-search";
    search.addEventListener("input", () => { state.search = search.value; state.page = 1; render(); });
    toolbar.appendChild(search);

    const sortSel = document.createElement("select");
    sortSel.className = "lib-sort";
    sortSel.innerHTML = `<option value="name">Name (A–Z)</option><option value="date">Zuletzt geändert</option><option value="size">Größe</option>`;
    sortSel.value = state.sort;
    sortSel.addEventListener("change", () => { state.sort = sortSel.value; render(); });
    toolbar.appendChild(sortSel);
    el.appendChild(toolbar);

    if (!state.search) renderBreadcrumb(el);

    if (!state.search) {
      const actionsRow = document.createElement("div");
      actionsRow.className = "lib-actions-row";
      const newFolderBtn = document.createElement("button");
      newFolderBtn.textContent = "+ Neuer Ordner";
      newFolderBtn.className = "btn-back";
      newFolderBtn.addEventListener("click", async () => {
        const name = prompt("Ordnername:");
        if (!name) return;
        const path = currentFolder() ? `${currentFolder()}/${name}` : name;
        const res = await api("/library/mkdir", { method: "POST", body: new URLSearchParams({ scope, path }) });
        if (res.ok) location.reload();
      });
      actionsRow.appendChild(newFolderBtn);
      el.appendChild(actionsRow);

      const folders = childFolders();
      if (folders.length) {
        const grid = document.createElement("div");
        grid.className = "folder-grid";
        folders.forEach((name) => {
          const card = document.createElement("div");
          card.className = "folder-card";
          const label = document.createElement("span");
          label.textContent = `📁 ${name} (${folderFileCount(name)})`;
          card.appendChild(label);
          const del = document.createElement("span");
          del.textContent = " 🗑";
          del.className = "folder-delete";
          del.addEventListener("click", async (ev) => {
            ev.stopPropagation();
            if (!confirm(`Ordner "${name}" inkl. Inhalt löschen?`)) return;
            const path = currentFolder() ? `${currentFolder()}/${name}` : name;
            const res = await api("/library/rmdir", { method: "POST", body: new URLSearchParams({ scope, path }) });
            if (res.ok) location.reload();
          });
          card.appendChild(del);
          card.addEventListener("click", () => { state.path = [...state.path, name]; state.page = 1; state.selected.clear(); render(); });
          grid.appendChild(card);
        });
        el.appendChild(grid);
      }
    }

    if (state.selected.size) {
      const bar = document.createElement("div");
      bar.className = "bulk-toolbar";
      bar.innerHTML = `<span>${state.selected.size} ausgewählt</span>`;
      const moveBtn = document.createElement("button");
      moveBtn.textContent = "📁➡ Verschieben";
      moveBtn.addEventListener("click", async () => {
        const dest = prompt("Zielordner (leer = Hauptordner):", currentFolder());
        if (dest === null) return;
        for (const path of state.selected) {
          const filename = path.split("/").pop();
          const dst = dest ? `${dest}/${filename}` : filename;
          await api("/library/move", { method: "POST", body: new URLSearchParams({ scope, src: path, dst }) });
        }
        location.reload();
      });
      const delBtn = document.createElement("button");
      delBtn.textContent = "🗑 Löschen";
      delBtn.className = "btn-delete";
      delBtn.addEventListener("click", async () => {
        if (!confirm(`${state.selected.size} Datei(en) wirklich löschen?`)) return;
        for (const path of state.selected) {
          await api("/library/delete", { method: "POST", body: new URLSearchParams({ scope, path }) });
        }
        location.reload();
      });
      const clearBtn = document.createElement("button");
      clearBtn.textContent = "Auswahl aufheben";
      clearBtn.className = "btn-back";
      clearBtn.addEventListener("click", () => { state.selected.clear(); render(); });
      bar.append(moveBtn, delBtn, clearBtn);
      el.appendChild(bar);
    }

    const list = visibleFiles();
    const totalPages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
    state.page = Math.min(state.page, totalPages);
    const pageItems = list.slice((state.page - 1) * PAGE_SIZE, state.page * PAGE_SIZE);

    const scrollWrap = document.createElement("div");
    scrollWrap.className = "table-scroll lib-dropzone";
    const table = document.createElement("table");
    table.className = "filelist";
    table.innerHTML = `<thead><tr><th></th><th>Datei</th><th>Größe</th><th>Geändert</th><th></th></tr></thead>
      <tbody>${pageItems.map(fileRowHtml).join("") || '<tr><td colspan="5" class="empty">Keine Dateien. Dateien können auch per Drag &amp; Drop hierher gezogen werden.</td></tr>'}</tbody>`;
    scrollWrap.appendChild(table);
    bindDragDrop(scrollWrap);
    el.appendChild(scrollWrap);

    $$(".row-check", table).forEach((cb) => {
      const path = cb.closest("tr").dataset.path;
      cb.checked = state.selected.has(path);
      cb.addEventListener("change", () => {
        if (cb.checked) state.selected.add(path); else state.selected.delete(path);
        render();
      });
    });
    $$(".btn-rename", table).forEach((btn) => {
      btn.addEventListener("click", async () => {
        const row = btn.closest("tr");
        const oldName = row.dataset.name;
        const newName = prompt("Neuer Dateiname:", oldName);
        if (!newName || newName === oldName) return;
        const folder = row.dataset.folder;
        const dst = folder ? `${folder}/${newName}` : newName;
        const res = await api("/library/move", { method: "POST", body: new URLSearchParams({ scope, src: row.dataset.path, dst }) });
        if (res.ok) location.reload();
      });
    });

    if (totalPages > 1) {
      const pag = document.createElement("div");
      pag.className = "pagination";
      for (let p = 1; p <= totalPages; p++) {
        const b = document.createElement("button");
        b.textContent = p;
        if (p === state.page) b.disabled = true;
        b.addEventListener("click", () => { state.page = p; render(); });
        pag.appendChild(b);
      }
      el.appendChild(pag);
    }
  }
  render();
}

// ---- Upload feedback: disable button + show status while the browser is
// sending the file(s), otherwise nothing visibly happens until the page
// reload lands (confusing for large files over a slow WLAN upload). -------
function initUploadForm() {
  const uploadForm = $("#uploadForm");
  if (!uploadForm || uploadForm.dataset.bound) return;
  uploadForm.dataset.bound = "1";
  uploadForm.addEventListener("submit", (e) => {
    const fileInput = uploadForm.querySelector('input[type="file"]');
    if (!fileInput.files.length) {
      e.preventDefault();
      toast("Bitte zuerst eine Datei auswählen.", true);
      return;
    }
    const btn = uploadForm.querySelector('button[type="submit"]');
    btn.disabled = true;
    btn.textContent = "Wird hochgeladen…";
  });
}
const MANUALS = [
  { label: "Manual I", defaultChannel: 0, pedal: false },
  { label: "Manual II", defaultChannel: 1, pedal: false },
  { label: "Pedal", defaultChannel: 2, pedal: true },
];

function noteRange(pedal) { return pedal ? [36, 67] : [48, 84]; }

async function sendNote(channels, note, velocity, on) {
  const list = Array.isArray(channels) ? channels : [channels];
  await Promise.all(list.map((channel) => api("/keyboard/note", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ channel, note, velocity, on })
  })));
}

function buildPianoKeyboard(container, channelsGetter, pedal) {
  const [lo, hi] = noteRange(pedal);
  const isBlack = (n) => [1, 3, 6, 8, 10].includes(n % 12);
  const whiteKeys = [];
  for (let n = lo; n <= hi; n++) if (!isBlack(n)) whiteKeys.push(n);
  const whiteWidth = 100 / whiteKeys.length;
  container.innerHTML = "";
  container.className = "piano" + (pedal ? " pedal" : "");
  let wi = 0;
  for (let n = lo; n <= hi; n++) {
    const black = isBlack(n);
    const key = document.createElement("div");
    key.className = "key " + (black ? "black" : "white");
    key.dataset.note = n;
    if (!black) { key.style.left = `${wi * whiteWidth}%`; key.style.width = `${whiteWidth}%`; wi++; }
    else { key.style.left = `${(wi - 0.3) * whiteWidth}%`; key.style.width = `${whiteWidth * 0.6}%`; }
    const press = (ev) => { ev.preventDefault(); sendNote(channelsGetter(), n, 100, true); key.classList.add("active"); };
    const release = () => { sendNote(channelsGetter(), n, 0, false); key.classList.remove("active"); };
    key.addEventListener("mousedown", press);
    key.addEventListener("mouseup", release);
    key.addEventListener("mouseleave", release);
    key.addEventListener("touchstart", press, { passive: false });
    key.addEventListener("touchend", release);
    container.appendChild(key);
  }
}

function buildManualsUI() {
  const card = $("#manualsCard");
  if (!card) return;
  card.innerHTML = `<h2>Manuale &amp; Pedal</h2>
    <p class="hint">Standardmäßig folgt jedes Manual automatisch der laufenden Wiedergabe. Häkchen
      entfernen, um einem Manual/Pedal fest einen oder mehrere Kanäle zuzuweisen (z.B. zum Koppeln) --
      diese Zuordnung gilt dann dauerhaft, unabhängig davon, was die Automatik sonst erkennen würde.</p>`;
  MANUAL_REFS = [];
  manualPins = [new Set(), new Set(), new Set()];
  MANUALS.forEach((m, i) => {
    const wrap = document.createElement("div");
    wrap.className = "manual-block";
    wrap.innerHTML = `<h3 class="manual-title">${m.label}</h3>
      <label class="manual-auto-toggle"><input type="checkbox" class="manual-auto-checkbox" checked>
        Automatisch (Wiedergabe folgen)</label>
      <label>Kanal-Zuordnung <span class="hint">(mehrere möglich = koppeln)</span></label>
      <select class="manual-channel" multiple size="4">${Array.from({ length: 16 }, (_, i2) =>
        `<option value="${i2}" ${i2 === m.defaultChannel ? "selected" : ""}>Kanal ${i2 + 1}</option>`).join("")}</select>
      <label>Register <span class="hint">(nur bei manueller Spielweise wählbar)</span></label>
      <select class="manual-register"></select>
      <div class="manual-reg-summary"></div>
      <div class="piano-container"></div>`;
    card.appendChild(wrap);
    const autoCb = wrap.querySelector(".manual-auto-checkbox");
    const chSel = wrap.querySelector(".manual-channel");
    const regSel = wrap.querySelector(".manual-register");
    const pianoEl = wrap.querySelector(".piano-container");
    const titleEl = wrap.querySelector(".manual-title");
    const regSummary = wrap.querySelector(".manual-reg-summary");

    const selectedChannels = () => [...chSel.selectedOptions].map((o) => parseInt(o.value));
    const syncPin = () => { manualPins[i] = autoCb.checked ? new Set() : new Set(selectedChannels().map(String)); };
    autoCb.addEventListener("change", syncPin);
    chSel.addEventListener("change", syncPin);

    buildPianoKeyboard(pianoEl, () => (manualPins[i].size ? [...manualPins[i]].map(Number) : selectedChannels()), m.pedal);
    regSel.addEventListener("change", () => {
      const [bank, program] = regSel.value.split(",").map(Number);
      (manualPins[i].size ? [...manualPins[i]].map(Number) : selectedChannels()).forEach((channel) => {
        api("/keyboard/program", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channel, bank, program })
        });
      });
    });
    MANUAL_REFS.push({ label: m.label, defaultChannel: m.defaultChannel, autoCb, chSel, regSel, regSummary, pianoEl, titleEl });
  });
  const overflow = document.createElement("div");
  overflow.id = "overflowVoices";
  card.appendChild(overflow);
  loadPresets().then(() => MANUAL_REFS.forEach((r) => buildGroupedSelect(r.regSel, 0, 0)));
  manualsFallback = MANUALS.map((m) => ({ channel: String(m.defaultChannel), label: m.label }));
  manualSlots = [null, null, null];
  api("/keyboard/ensure_started", { method: "POST" });
}

// ---- Dynamic manual/pedal <-> MIDI-channel mapping -------------------------
// Each manual/pedal defaults to "automatic": it follows whichever channel is
// actually sounding right now, picking the lowest-pitched active voice for
// the Pedal slot. Unchecking a manual's "Automatisch" box turns it into an
// explicit, permanent pin instead (one or several channels, e.g. to couple
// two divisions together) -- pinned channels are never touched by the
// automatic assignment and are excluded from what the *other* (still
// automatic) manuals compete over.
//
// Two things that would otherwise make automatic assignment jittery/wrong:
// - Organ MIDI exports frequently duplicate one musical voice onto two
//   channels (identical notes, same timing -- a coupler/second registration
//   artifact). Treating those as two separate voices burns two of the three
//   slots for what is really one voice, so real distinct voices end up
//   fighting over whatever's left. inspect_channels() already detects this
//   (voice_group) -- channels that aren't their own group's leader are
//   skipped here entirely.
// - Normal phrasing has brief rests (a channel's active-note set going
//   empty for a fraction of a second). Freeing a slot the instant that
//   happens causes constant, meaningless reassignment. A short grace period
//   keeps a slot with its channel through brief gaps and only actually
//   frees it once the channel has been silent for a while.
let manualSlots = [null, null, null];        // slot index -> auto-assigned channel(string) or null
let manualPins = [new Set(), new Set(), new Set()];  // slot index -> explicitly pinned channel(string) set
let MANUAL_REFS = [];
let lastActiveAt = {};                  // channel(string) -> Date.now() it was last sounding
const PEDAL_SLOT = 2;
const PEDAL_PITCH_THRESHOLD = 55;       // avg MIDI note below this reads as a bass/pedal line
const SLOT_RELEASE_GRACE_MS = 1000;     // bridges brief rests without reshuffling the display

function assignManualSlots(channels, active) {
  const now = Date.now();
  const pinnedElsewhere = new Set([...manualPins[0], ...manualPins[1], ...manualPins[2]]);

  // Pinned slots aren't managed by auto-assignment. A channel that has
  // since been pinned to some slot also loses its old (now stale) auto-slot.
  manualSlots = manualSlots.map((ch, i) => {
    if (manualPins[i].size > 0) return null;
    if (ch && pinnedElsewhere.has(ch)) return null;
    return ch;
  });

  const voices = channels.filter((c) => c.voice_group === c.channel && !pinnedElsewhere.has(c.channel));
  const activeVoices = voices.filter((c) => active[c.channel] && active[c.channel].size > 0);
  activeVoices.forEach((c) => { lastActiveAt[c.channel] = now; });

  // Free a slot only once its channel has been silent longer than the grace
  // period -- not the instant it goes quiet.
  manualSlots = manualSlots.map((ch, i) => {
    if (manualPins[i].size > 0 || !ch) return null;
    return (now - (lastActiveAt[ch] || 0) <= SLOT_RELEASE_GRACE_MS) ? ch : null;
  });

  // Voices sounding now but without a slot yet -- lowest average pitch
  // first, so a genuine pedal line wins the Pedal slot over a manual voice
  // that merely happens to free up first. Pinned slots are never candidates.
  const unassigned = activeVoices
    .filter((c) => !manualSlots.includes(c.channel))
    .sort((a, b) => (a.avg_note ?? 999) - (b.avg_note ?? 999));

  const slotFree = (i) => manualPins[i].size === 0 && manualSlots[i] === null;
  const overflow = [];
  unassigned.forEach((c) => {
    const looksLikePedal = c.avg_note != null && c.avg_note < PEDAL_PITCH_THRESHOLD;
    let slot = -1;
    if (looksLikePedal && slotFree(PEDAL_SLOT)) slot = PEDAL_SLOT;
    else if (slotFree(0)) slot = 0;
    else if (slotFree(1)) slot = 1;
    else if (slotFree(PEDAL_SLOT)) slot = PEDAL_SLOT;
    if (slot === -1) overflow.push(c);
    else manualSlots[slot] = c.channel;
  });
  return overflow;
}

function highlightKeys(pianoEl, notesSet) {
  $$(".key", pianoEl).forEach((k) => {
    k.classList.toggle("active", !!(notesSet && notesSet.has(parseInt(k.dataset.note))));
  });
}

function setManualsAutoMode(filePlaying) {
  // The channel picker (and its auto/pin checkbox) stay usable at all times
  // -- pins are the user's own configuration, independent of what's playing.
  // Only the register picker is tied to playback: while a file plays, the
  // sounding register comes from the file/engine, not from this dropdown.
  MANUAL_REFS.forEach((r) => { r.regSel.disabled = filePlaying; });
}

function renderOverflow(list) {
  const el = $("#overflowVoices");
  if (!el) return;
  if (!list.length) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="hint" style="margin-top:16px">Weitere gleichzeitig aktive Stimmen (mehr als die 3 Manuale/Pedal fassen):</div>`
    + list.map((c) => `<div class="vis-row lit"><span class="vis-dot"></span>
        <span class="vis-label">${c.label}</span><span class="vis-reg">${c.name}</span></div>`).join("");
}

// Slow path (runs from the 400ms status tick): decide WHICH channel(s)
// occupy each non-pinned slot, and update the text/dropdown bits that don't
// need per-frame smoothness. Key highlighting itself is handled by
// pianoKeyFrame() below, continuously, so it doesn't feel sluggish even
// though slot assignment only re-evaluates a few times a second.
function updateManualsFromPlayback(channels, chstate, active, filePlaying) {
  if (!MANUAL_REFS.length) return;
  setManualsAutoMode(filePlaying);
  if (!filePlaying) {
    if (manualSlots.some((s) => s !== null)) manualSlots = [null, null, null];
    lastActiveAt = {};
    renderOverflow([]);
    MANUAL_REFS.forEach((r) => { r.titleEl.textContent = r.label; r.regSummary.textContent = ""; });
    return;
  }
  const overflow = assignManualSlots(channels, active);
  MANUAL_REFS.forEach((ref, i) => {
    const chs = ref.autoCb.checked ? (manualSlots[i] ? [manualSlots[i]] : []) : [...manualPins[i]];
    if (chs.length) {
      const regs = chs.map((ch) => {
        const st = chstate[ch] || { bank: 0, program: 0 };
        return `K${parseInt(ch) + 1}: ${presetName(st.bank, st.program)}`;
      });
      ref.titleEl.textContent = chs.length === 1
        ? `${ref.label} — Kanal ${parseInt(chs[0]) + 1}`
        : `${ref.label} — Kanäle ${chs.map((c) => parseInt(c) + 1).join("+")}`;
      ref.regSummary.textContent = regs.join(" · ");
    } else {
      ref.titleEl.textContent = ref.label;
      ref.regSummary.textContent = ref.autoCb.checked ? "" : "Kein Kanal zugeordnet";
    }
  });
  renderOverflow(overflow.map((c) => {
    const st = chstate[c.channel] || { bank: 0, program: 0 };
    return { label: c.label, name: presetName(st.bank, st.program) };
  }));
}

// Fast path: every animation frame, re-derive "what's sounding now" (cheap,
// no network -- computeActiveNotes() reads the web-preview event timeline
// against the AudioContext clock, or the last-polled status for realtime
// playback) and paint the keys. This is what makes key highlighting feel
// immediate instead of updating only 2-3 times a second. Merges notes
// across every channel currently assigned to a manual (auto or pinned/
// coupled), so a coupled manual lights up for either channel.
function pianoKeyFrame() {
  if (MANUAL_REFS.length) {
    const active = computeActiveNotes();
    MANUAL_REFS.forEach((ref, i) => {
      const chs = ref.autoCb.checked ? (manualSlots[i] ? [manualSlots[i]] : []) : [...manualPins[i]];
      if (chs.length <= 1) {
        highlightKeys(ref.pianoEl, active[chs[0]]);
      } else {
        const merged = new Set();
        chs.forEach((ch) => { const s = active[ch]; if (s) s.forEach((n) => merged.add(n)); });
        highlightKeys(ref.pianoEl, merged);
      }
    });
  }
  requestAnimationFrame(pianoKeyFrame);
}
requestAnimationFrame(pianoKeyFrame);

// ---- Browser Web MIDI (Geräte am Client statt am Pi selbst) ---------------
function initWebMidiButton() {
  const btn = $("#enableWebMidi");
  if (!btn || btn.dataset.bound) return;
  btn.dataset.bound = "1";
  btn.addEventListener("click", async () => {
    if (!navigator.requestMIDIAccess) { alert("Web MIDI wird von diesem Browser nicht unterstützt."); return; }
    const access = await navigator.requestMIDIAccess();
    const list = $("#webMidiList");
    list.innerHTML = "";
    for (const input of access.inputs.values()) {
      const row = document.createElement("div");
      row.className = "webmidi-row";
      row.innerHTML = `<span>${input.name}</span> → Kanal
        <select>${Array.from({ length: 16 }, (_, i) => `<option value="${i}">${i + 1}</option>`).join("")}</select>`;
      const sel = row.querySelector("select");
      input.onmidimessage = (ev) => {
        const [status, note, velocity] = ev.data;
        const type = status & 0xf0;
        const channel = parseInt(sel.value);
        if (type === 0x90 && velocity > 0) sendNote(channel, note, velocity, true);
        else if (type === 0x80 || (type === 0x90 && velocity === 0)) sendNote(channel, note, 0, false);
      };
      list.appendChild(row);
    }
  });
}

// ---- Soft navigation: swap #pageMain via fetch instead of a full page load
// so the nav-bar, transport bar, AudioContext and playback state above all
// survive switching between Bibliothek and Virtuelle Orgel -- previously a
// full reload tore down the <audio> element and all JS state, silently
// killing web-preview playback every time you switched pages. --------------
function initPage() {
  $$(".libbrowser").forEach(initLibBrowser);
  initUploadForm();
  if ($("#manualsCard")) buildManualsUI();
  initAddRouteButton();
  initWebMidiButton();
}

async function softNavigate(url, push = true) {
  let html;
  try {
    const res = await fetch(url, { headers: { "X-Soft-Nav": "1" } });
    if (!res.ok) { location.href = url; return; }
    html = await res.text();
  } catch (e) {
    location.href = url; // offline/unreachable: fall back to a real navigation
    return;
  }
  const doc = new DOMParser().parseFromString(html, "text/html");
  const newMain = doc.querySelector("#pageMain");
  const target = $("#pageMain");
  if (!newMain || !target) { location.href = url; return; }
  target.innerHTML = newMain.innerHTML;
  if (doc.title) document.title = doc.title;
  $$(".nav-links a[data-soft-nav]").forEach((a) => {
    a.classList.toggle("active", new URL(a.href, location.href).pathname === new URL(url, location.href).pathname);
  });
  if (push) history.pushState({ softNav: true }, "", url);
  initPage();
  window.scrollTo(0, 0);
}

document.addEventListener("click", (e) => {
  const link = e.target.closest("a[data-soft-nav]");
  if (!link) return;
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return; // let the browser handle "open in new tab" etc.
  e.preventDefault();
  if (link.pathname === location.pathname) return;
  softNavigate(link.href);
});

window.addEventListener("popstate", () => softNavigate(location.href, false));

initPage();
