#!/usr/bin/env python3
"""Film Metadata Mapper.

Local web app to pair scanned images with entries of an exiftool-style JSON
(e.g. a Lightme Logbook export) and write the metadata into the images,
either in place or into tagged copies. Uses the ``exiftool`` CLI for writing.

Run:  virtualenv/bin/python meta_mapper.py  ->  http://127.0.0.1:5000
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any

from flask import Flask, Response, abort, jsonify, request, send_from_directory

app = Flask(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic", ".dng"}
BROWSER_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Single-user local tool: keep the loaded state in memory.
STATE: dict[str, Any] = {"entries": [], "json_name": "", "img_dir": os.getcwd()}


def natural_key(name: str) -> list:
    """Sort key that orders 'IMG_2' before 'IMG_10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def list_images(directory: str) -> list[str]:
    """Return image file names (not paths) in ``directory``, naturally sorted."""
    names = [
        n for n in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, n))
        and os.path.splitext(n)[1].lower() in IMAGE_EXTS
    ]
    return sorted(names, key=natural_key)


def image_info(directory: str) -> list[dict]:
    """List images with file stats and EXIF date/dimensions (one exiftool call)."""
    names = list_images(directory)
    exif: dict[str, dict] = {}
    if names and shutil.which("exiftool"):
        proc = subprocess.run(
            ["exiftool", "-j", "-fast2", "-charset", "filename=utf8",
             "-DateTimeOriginal", "-CreateDate", "-ImageWidth", "-ImageHeight",
             *[os.path.join(directory, n) for n in names]],
            capture_output=True, text=True)
        try:
            for rec in json.loads(proc.stdout or "[]"):
                exif[os.path.basename(rec.get("SourceFile", ""))] = rec
        except ValueError:
            pass
    out = []
    for n in names:
        st = os.stat(os.path.join(directory, n))
        e = exif.get(n, {})
        date = str(e.get("DateTimeOriginal") or e.get("CreateDate") or "").strip()
        out.append({"name": n, "mtime": st.st_mtime, "size": st.st_size,
                    "exifDate": date if date and not date.startswith("0000") else "",
                    "w": e.get("ImageWidth"), "h": e.get("ImageHeight")})
    return out


def safe_image_path(name: str) -> str:
    """Resolve an image name inside the current image dir, rejecting traversal."""
    base = os.path.realpath(STATE["img_dir"])
    path = os.path.realpath(os.path.join(base, name))
    if os.path.dirname(path) != base or not os.path.isfile(path):
        raise ValueError(f"Invalid image: {name}")
    return path


def exif_record(entry: dict, tags: list[str], set_create_date: bool) -> dict:
    """Build an exiftool JSON record from a JSON entry.

    Numeric values get a '#' suffix so exiftool writes them as raw values;
    otherwise enum tags like FileSource=1 would be rejected.
    """
    rec: dict[str, Any] = {}
    for key in tags:
        if key == "SourceFile" or key not in entry:
            continue
        val = entry[key]
        if val is None or isinstance(val, dict):
            continue
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            rec[f"{key}#"] = val
        else:
            rec[key] = val
    if set_create_date and entry.get("DateTimeOriginal"):
        rec["CreateDate"] = entry["DateTimeOriginal"]
    return rec


def target_name(entry: dict, index: int, src_name: str, rename: str) -> str:
    """Output file name for copy mode."""
    stem, ext = os.path.splitext(src_name)
    frame = entry.get("ImageNumber", index + 1)
    try:
        frame_str = f"{int(frame):03d}"
    except (TypeError, ValueError):
        frame_str = f"{index + 1:03d}"
    if rename == "frame":
        return f"{frame_str}{ext}"
    if rename == "frame_orig":
        return f"{frame_str}_{stem}{ext}"
    return src_name


# ----------------------------------------------------------------- API routes

@app.post("/api/load_json")
def load_json():
    """Load the metadata JSON from an uploaded file or a server-side path."""
    try:
        if "file" in request.files:
            f = request.files["file"]
            data = json.loads(f.read().decode("utf-8"))
            name = f.filename or "uploaded.json"
        else:
            path = os.path.expanduser((request.json or {}).get("path", "").strip())
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            name = os.path.abspath(path)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return jsonify(error=f"Could not load JSON: {exc}"), 400
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
        return jsonify(error="JSON must be a list of objects"), 400
    STATE["entries"], STATE["json_name"] = data, name
    return jsonify(name=name, entries=data)


@app.post("/api/scan")
def scan():
    """Set the image directory and list its images."""
    directory = os.path.expanduser((request.json or {}).get("dir", "").strip()) or os.getcwd()
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        return jsonify(error=f"Not a directory: {directory}"), 400
    STATE["img_dir"] = directory
    return jsonify(dir=directory, images=image_info(directory))


@app.get("/api/state")
def state():
    """Return current server state (used on page load)."""
    d = STATE["img_dir"]
    return jsonify(name=STATE["json_name"], entries=STATE["entries"], dir=d,
                   images=image_info(d) if os.path.isdir(d) else [],
                   exiftool=bool(shutil.which("exiftool")))


@app.get("/img/<path:name>")
def image(name: str):
    """Serve an image; for non-browser formats try an embedded JPEG preview."""
    try:
        path = safe_image_path(name)
    except ValueError:
        abort(404)
    if os.path.splitext(path)[1].lower() in BROWSER_EXTS:
        return send_from_directory(STATE["img_dir"], os.path.basename(path))
    if shutil.which("exiftool"):
        for tag in ("-PreviewImage", "-JpgFromRaw", "-ThumbnailImage"):
            out = subprocess.run(["exiftool", "-b", tag, path], capture_output=True)
            if out.stdout:
                return Response(out.stdout, mimetype="image/jpeg")
    abort(404)


@app.post("/api/export")
def export():
    """Download the JSON with SourceFile replaced by the paired image names."""
    pairs = {int(k): v for k, v in (request.json or {}).get("pairs", {}).items()}
    out = []
    for i, entry in enumerate(STATE["entries"]):
        e = dict(entry)
        if i in pairs:
            e["SourceFile"] = os.path.join(STATE["img_dir"], pairs[i])
        out.append(e)
    body = json.dumps(out, indent=2, ensure_ascii=False)
    return Response(body, mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=paired.json"})


@app.post("/api/write")
def write():
    """Write metadata into paired images (in place or as copies) via exiftool."""
    if not shutil.which("exiftool"):
        return jsonify(error="exiftool not found in PATH"), 500
    req = request.json or {}
    pairs = {int(k): v for k, v in req.get("pairs", {}).items()}
    tags: list[str] = req.get("tags", [])
    mode = req.get("mode", "copy")
    rename = req.get("rename", "keep")
    entries = STATE["entries"]
    if not pairs:
        return jsonify(error="Nothing is paired"), 400

    out_dir = None
    if mode == "copy":
        out_dir = os.path.abspath(os.path.expanduser(req.get("outDir", "").strip() or
                                                     os.path.join(STATE["img_dir"], "tagged")))
        if rename == "keep" and os.path.realpath(out_dir) == os.path.realpath(STATE["img_dir"]):
            return jsonify(error="Output folder equals image folder; choose another folder "
                                 "or a renaming option"), 400
        os.makedirs(out_dir, exist_ok=True)

    records, files, seen = [], [], set()
    try:
        for idx in sorted(pairs):
            if not 0 <= idx < len(entries):
                raise ValueError(f"Bad entry index {idx}")
            src = safe_image_path(pairs[idx])
            if mode == "copy":
                dst = os.path.join(out_dir, target_name(entries[idx], idx, pairs[idx], rename))
                if dst in seen:
                    raise ValueError(f"Two images would be written to {os.path.basename(dst)}")
                seen.add(dst)
                shutil.copy2(src, dst)
            else:
                dst = src
            rec = exif_record(entries[idx], tags, bool(req.get("createDate")))
            rec["SourceFile"] = dst
            records.append(rec)
            files.append(dst)
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(records, tmp, ensure_ascii=False)
        tmp_path = tmp.name
    cmd = ["exiftool", "-charset", "filename=utf8", f"-json={tmp_path}"]
    if mode == "copy" or not req.get("backup", True):
        cmd.append("-overwrite_original")
    try:
        proc = subprocess.run(cmd + files, capture_output=True, text=True)
    finally:
        os.unlink(tmp_path)
    return jsonify(ok=proc.returncode == 0, output=(proc.stdout + proc.stderr).strip(),
                   count=len(files), outDir=out_dir)


@app.get("/")
def index():
    """Serve the single-page UI."""
    return Response(HTML, mimetype="text/html")


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Film Metadata Mapper</title>
<style>
  :root { --bg:#1e1e2e; --panel:#313244; --card:#45475a; --text:#cdd6f4; --muted:#a6adc8;
          --accent:#89b4fa; --ok:#a6e3a1; --warn:#f9e2af; --err:#f38ba8; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, sans-serif; background:var(--bg); color:var(--text); }
  header { position:sticky; top:0; z-index:5; background:#181825; padding:10px 16px;
           display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  header input[type=text] { width:280px; }
  input, select { background:var(--panel); color:var(--text); border:1px solid #585b70;
                  border-radius:5px; padding:6px; }
  button { background:var(--accent); color:#11111b; border:none; padding:7px 12px;
           border-radius:6px; font-weight:600; cursor:pointer; }
  button.secondary { background:var(--card); color:var(--text); }
  button:focus-visible, .entry:focus-visible, .thumb:focus-visible { outline:2px solid var(--warn); }
  .sep { width:1px; height:28px; background:#585b70; margin:0 4px; }
  #status { margin-left:auto; color:var(--muted); }
  main { display:grid; grid-template-columns: 3fr 2fr; gap:16px; padding:16px; }
  .col { background:var(--panel); border-radius:8px; padding:12px; }
  .col h3 { margin:0 0 10px; display:flex; gap:10px; align-items:center; }
  #pool-col { position:sticky; top:70px; max-height:calc(100vh - 90px); overflow:auto; }
  .entry { display:flex; gap:12px; background:var(--card); border-radius:6px; padding:8px;
           margin-bottom:8px; border:2px solid transparent; cursor:pointer; }
  .entry.selected { border-color:var(--accent); }
  .entry.over, #pool.over { border-color:var(--warn); border-style:dashed; }
  .entry.paired .drop { border-style:solid; border-color:var(--ok); }
  .drop { width:150px; height:100px; flex:none; border:2px dashed #6c7086; border-radius:4px;
          display:flex; align-items:center; justify-content:center; position:relative;
          color:var(--muted); font-size:12px; background:#181825; }
  .drop img { width:100%; height:100%; object-fit:contain; }
  .drop .x { position:absolute; top:2px; right:2px; padding:0 6px; background:var(--err); }
  .meta { font-size:13px; line-height:1.5; }
  .meta b { font-size:15px; }
  .meta .muted { color:var(--muted); }
  :root { --thumb: 130px; }
  .toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:10px; font-size:13px; }
  .toolbar input[type=search] { flex:1; min-width:140px; }
  #pool { display:grid; grid-template-columns:repeat(auto-fill,minmax(var(--thumb),1fr)); gap:8px;
          min-height:200px; border:2px solid transparent; border-radius:6px; }
  .thumb { background:var(--card); border-radius:6px; padding:4px; cursor:grab; font-size:11px;
           text-align:center; word-break:break-all; }
  .thumb img, .thumb .noprev { width:100%; height:calc(var(--thumb) * .7); object-fit:contain;
           background:#181825; display:block; }
  .thumb .sub { color:var(--muted); }
  .thumb.used { opacity:.35; }
  .thumb .badge { color:var(--ok); }
  .noprev { height:90px; display:flex; align-items:center; justify-content:center;
            background:#181825; color:var(--muted); }
  dialog { background:var(--panel); color:var(--text); border:none; border-radius:10px;
           max-width:760px; width:90%; }
  dialog::backdrop { background:rgba(0,0,0,.6); }
  #tags { columns:3; font-size:13px; margin:8px 0; }
  #tags label { display:block; }
  fieldset { border:1px solid #585b70; border-radius:6px; margin:10px 0; }
  pre { background:#11111b; padding:10px; border-radius:6px; max-height:240px; overflow:auto;
        white-space:pre-wrap; }
  #lightbox { max-width:95vw; width:auto; padding:8px; }
  #lightbox img { max-width:90vw; max-height:85vh; display:block; }
  .err { color:var(--err); }
</style>
</head>
<body>
<header>
  <input type="file" id="json-file" accept=".json,application/json" aria-label="Upload JSON">
  <input type="text" id="json-path" placeholder="…or JSON path on disk" aria-label="JSON path">
  <button class="secondary" onclick="loadJsonPath()">Load JSON</button>
  <span class="sep"></span>
  <input type="text" id="img-dir" placeholder="Image folder" aria-label="Image folder">
  <button class="secondary" onclick="scanDir()">Scan folder</button>
  <span class="sep"></span>
  <button onclick="autoPair()" title="Pairs unpaired entries (from the selected one) with the unpaired images in the current pool order/filter">Auto-pair</button>
  <button class="secondary" onclick="undo()" title="Undo (Ctrl+Z)">Undo</button>
  <button class="secondary" onclick="clearPairs()">Clear</button>
  <button class="secondary" onclick="exportJson()">Download paired JSON</button>
  <button onclick="openWrite()">Write metadata…</button>
  <span id="status"></span>
</header>

<main>
  <section class="col">
    <h3>JSON entries <small id="json-name" class="muted"></small></h3>
    <div class="toolbar">
      <input type="search" id="entry-filter" placeholder="Filter entries (date, #, notes…)" oninput="renderEntries()" aria-label="Filter entries">
      <label><input type="checkbox" id="unpaired-only" onchange="renderEntries()"> unpaired only</label>
      <button class="secondary" onclick="jumpUnpaired()" title="Select next unpaired entry (N)">Next unpaired</button>
    </div>
    <div id="entries"></div>
  </section>
  <section class="col" id="pool-col">
    <h3>Images <small id="pool-count" class="muted"></small></h3>
    <div class="toolbar">
      <label>Sort
        <select id="sort" onchange="savePrefs(); renderPool()">
          <option value="name">Name A→Z</option>
          <option value="name-desc">Name Z→A</option>
          <option value="mtime">Modified (oldest first)</option>
          <option value="mtime-desc">Modified (newest first)</option>
          <option value="exif">EXIF date (oldest first)</option>
          <option value="exif-desc">EXIF date (newest first)</option>
          <option value="size">Size (smallest first)</option>
          <option value="size-desc">Size (largest first)</option>
        </select></label>
      <input type="search" id="pool-filter" placeholder="Filter by name" oninput="renderPool()" aria-label="Filter images">
      <label><input type="checkbox" id="hide-used" onchange="savePrefs(); renderPool()"> hide paired</label>
      <label>Size <input type="range" id="thumb-size" min="90" max="320" value="130" oninput="applyThumbSize(); savePrefs()" aria-label="Thumbnail size"></label>
    </div>
    <div id="pool"></div>
    <p class="muted" style="font-size:12px">Drag an image onto an entry, or select an entry and click an image.
      Drag a paired image back here to unpair. Double-click to enlarge (←/→ to browse).<br>
      Keys: ↑/↓ select entry · Del unpair selected · N next unpaired · Ctrl+Z undo</p>
  </section>
</main>

<dialog id="write-dlg">
  <h3>Write metadata</h3>
  <fieldset><legend>Mode</legend>
    <label><input type="radio" name="mode" value="copy" checked> Write to copies in folder:</label>
    <input type="text" id="out-dir" style="width:60%"><br>
    &nbsp;&nbsp;&nbsp;Copy names:
    <select id="rename">
      <option value="keep">keep original name</option>
      <option value="frame">frame number (001.jpg)</option>
      <option value="frame_orig">frame + original (001_IMG_5714.JPG)</option>
    </select><br>
    <label><input type="radio" name="mode" value="inplace"> Modify original files in place</label>
    <label><input type="checkbox" id="backup" checked> keep <code>_original</code> backup</label>
  </fieldset>
  <fieldset><legend>Tags
    <button type="button" class="secondary" onclick="setAllTags(true)">all</button>
    <button type="button" class="secondary" onclick="setAllTags(false)">none</button></legend>
    <div id="tags"></div>
    <label><input type="checkbox" id="create-date" checked> also set CreateDate from DateTimeOriginal</label>
  </fieldset>
  <p id="write-summary" class="muted"></p>
  <pre id="write-out" hidden></pre>
  <button onclick="doWrite()" id="write-btn">Write</button>
  <button class="secondary" onclick="document.getElementById('write-dlg').close()">Close</button>
</dialog>

<dialog id="lightbox" onclick="this.close()"><img alt=""><p id="lb-cap" style="margin:4px 0 0"></p></dialog>

<script>
let entries = [], images = [], imgDir = '', jsonName = '';
let infoByName = {};     // filename -> {name, mtime, size, exifDate, w, h}
let pairs = {};          // entry index -> image filename
let selected = null;     // selected entry index
let history = [];        // undo stack of pair snapshots
let lbFiles = [], lbPos = 0;

const $ = id => document.getElementById(id);
const el = (tag, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(tag), props);
  kids.forEach(k => n.append(k));
  return n;
};
const imgUrl = f => '/img/' + encodeURIComponent(f);
const storeKey = () => 'mm:' + jsonName + '|' + imgDir;
const imageNames = () => images.map(i => i.name);

async function api(url, body, isForm) {
  const opts = { method: 'POST' };
  if (isForm) opts.body = body;
  else { opts.headers = {'Content-Type': 'application/json'}; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  const d = await r.json();
  if (!r.ok) { alert(d.error || 'Request failed'); throw new Error(d.error); }
  return d;
}

// ---------------------------------------------------------------- prefs
function savePrefs() {
  localStorage.setItem('mm:prefs', JSON.stringify({
    sort: $('sort').value, hide: $('hide-used').checked, size: $('thumb-size').value}));
}
function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem('mm:prefs') || '{}');
    if (p.sort) $('sort').value = p.sort;
    if (p.size) $('thumb-size').value = p.size;
    $('hide-used').checked = !!p.hide;
  } catch (e) {}
  applyThumbSize();
}
function applyThumbSize() { document.documentElement.style.setProperty('--thumb', $('thumb-size').value + 'px'); }

function setImages(list) {
  images = list; infoByName = Object.fromEntries(list.map(i => [i.name, i]));
}
function restorePairs() {
  pairs = {}; history = [];
  try { pairs = JSON.parse(localStorage.getItem(storeKey()) || '{}'); } catch (e) {}
  for (const k of Object.keys(pairs))       // drop stale pairs
    if (!(+k < entries.length) || !infoByName[pairs[k]]) delete pairs[k];
  selected = entries.length ? 0 : null;
}
function persist() { localStorage.setItem(storeKey(), JSON.stringify(pairs)); }
function snapshot() { history.push(JSON.stringify(pairs)); if (history.length > 200) history.shift(); }
function undo() {
  if (!history.length) return flash('Nothing to undo');
  pairs = JSON.parse(history.pop()); persist(); render(); flash('Undone');
}
function flash(msg) {
  const s = $('status'), old = s.dataset.base || '';
  s.textContent = msg; clearTimeout(flash.t);
  flash.t = setTimeout(() => s.textContent = old, 1200);
}

function onJsonLoaded(d) {
  entries = d.entries; jsonName = d.name;
  $('json-name').textContent = '— ' + jsonName;
  restorePairs(); render();
}
$('json-file').addEventListener('change', async e => {
  const f = e.target.files[0]; if (!f) return;
  const fd = new FormData(); fd.append('file', f);
  onJsonLoaded(await api('/api/load_json', fd, true));
});
async function loadJsonPath() { onJsonLoaded(await api('/api/load_json', {path: $('json-path').value})); }
async function scanDir() {
  $('status').textContent = 'Scanning…';
  const d = await api('/api/scan', {dir: $('img-dir').value});
  imgDir = d.dir; setImages(d.images); $('img-dir').value = imgDir;
  restorePairs(); render();
}

// ---------------------------------------------------------------- pairing
function fileOwner(file) {
  for (const k in pairs) if (pairs[k] === file) return +k;
  return null;
}
function assign(idx, file, fromIdx = null) {
  snapshot();
  const prevOwner = fileOwner(file);
  const displaced = pairs[idx];
  if (prevOwner !== null) delete pairs[prevOwner];
  pairs[idx] = file;
  // dragging between entries swaps their images
  if (displaced && displaced !== file && fromIdx !== null && fromIdx !== idx) pairs[fromIdx] = displaced;
  persist(); render();
}
function unassignIdx(i) { if (pairs[i]) { snapshot(); delete pairs[i]; persist(); render(); } }
function unassignFile(file) { const o = fileOwner(file); if (o !== null) unassignIdx(o); }
function nextUnpaired(from) {
  for (let i = from; i < entries.length; i++) if (!pairs[i]) return i;
  for (let i = 0; i < from; i++) if (!pairs[i]) return i;
  return null;
}
function jumpUnpaired() {
  const n = nextUnpaired((selected ?? -1) + 1);
  if (n === null) return flash('All entries are paired');
  select(n);
}
function select(i) {
  selected = i; renderEntries();
  document.querySelector('.entry.selected')?.scrollIntoView({block: 'nearest'});
}

const SORTS = {
  'name': (a, b) => naturalCmp(a.name, b.name),
  'mtime': (a, b) => a.mtime - b.mtime || naturalCmp(a.name, b.name),
  // images without EXIF date go last
  'exif': (a, b) => (!a.exifDate - !b.exifDate) || a.exifDate.localeCompare(b.exifDate) || naturalCmp(a.name, b.name),
  'size': (a, b) => a.size - b.size,
};
const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: 'base'});
function naturalCmp(a, b) { return collator.compare(a, b); }
function poolOrder() {
  const [key, dir] = $('sort').value.split('-');
  const sorted = [...images].sort(SORTS[key]);
  if (dir === 'desc') sorted.reverse();
  const q = $('pool-filter').value.trim().toLowerCase();
  return sorted.map(i => i.name).filter(n => !q || n.toLowerCase().includes(q));
}
function autoPair() {
  snapshot();
  const used = new Set(Object.values(pairs));
  const free = poolOrder().filter(f => !used.has(f));
  let i = selected ?? 0, n = 0;
  for (const f of free) {
    while (i < entries.length && pairs[i]) i++;
    if (i >= entries.length) break;
    pairs[i++] = f; n++;
  }
  persist(); render(); flash(`Auto-paired ${n} image(s)`);
}
function clearPairs() { if (confirm('Remove all pairings? (Undo is available)')) { snapshot(); pairs = {}; persist(); render(); } }

// ---------------------------------------------------------------- rendering
function fmtExposure(t) {
  if (typeof t !== 'number') return t ?? '';
  return t >= 1 ? t + 's' : '1/' + Math.round(1 / t);
}
function fmtSize(b) { return b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.round(b / 1024) + ' kB'; }
function fmtTime(s) { return new Date(s * 1000).toLocaleString(); }
function setDragData(e, file, fromIdx) {
  e.dataTransfer.setData('text/plain', JSON.stringify({file, fromIdx}));
  e.dataTransfer.effectAllowed = 'move';
}
function readDrag(e) { try { return JSON.parse(e.dataTransfer.getData('text/plain')); } catch (_) { return null; } }
function dropZone(node, onDrop) {
  node.addEventListener('dragover', e => { e.preventDefault(); node.classList.add('over'); });
  node.addEventListener('dragleave', () => node.classList.remove('over'));
  node.addEventListener('drop', e => { e.preventDefault(); node.classList.remove('over');
    const d = readDrag(e); if (d) onDrop(d); });
}
function makeImg(file, gallery) {
  const img = el('img', {src: imgUrl(file), alt: file, loading: 'lazy', draggable: false});
  img.onerror = () => img.replaceWith(el('div', {className: 'noprev', textContent: 'no preview'}));
  img.ondblclick = e => { e.stopPropagation(); showLightbox(file, gallery); };
  return img;
}
function entryText(e, i) {
  return [`#${e.ImageNumber ?? i + 1}`, e.DateTimeOriginal, e.Notes, e.SourceFile, pairs[i]]
    .filter(Boolean).join(' ').toLowerCase();
}

function renderEntries() {
  const box = $('entries'); box.replaceChildren();
  const q = $('entry-filter').value.trim().toLowerCase();
  const onlyUnpaired = $('unpaired-only').checked;
  const paired = entries.map((_, i) => pairs[i]).filter(Boolean);
  entries.forEach((e, i) => {
    const file = pairs[i];
    if (onlyUnpaired && file && i !== selected) return;
    if (q && !entryText(e, i).includes(q)) return;
    const drop = el('div', {className: 'drop'});
    if (file) {
      const img = makeImg(file, paired);
      drop.draggable = true;
      drop.addEventListener('dragstart', ev => setDragData(ev, file, i));
      const x = el('button', {className: 'x', textContent: '×', title: 'Unpair', ariaLabel: 'Unpair'});
      x.onclick = ev => { ev.stopPropagation(); unassignIdx(i); };
      drop.append(img, x);
    } else drop.textContent = 'drop image';

    const gps = e.GPSLatitude ? `${e.GPSLatitude}, ${e.GPSLongitude ?? ''}` : '';
    const meta = el('div', {className: 'meta'},
      el('b', {textContent: `#${e.ImageNumber ?? i + 1}`}), '  ',
      el('span', {textContent: e.DateTimeOriginal ?? ''}), el('br'),
      el('span', {textContent: [fmtExposure(e.ExposureTime), e.FNumber && 'f/' + e.FNumber,
        e.FocalLength && e.FocalLength + 'mm', e.ISO && 'ISO ' + e.ISO].filter(Boolean).join(' · ')}), el('br'),
      el('span', {className: 'muted', textContent: gps}), el('br'),
      el('span', {className: 'muted', textContent: (e.SourceFile ?? '') + (file ? '  →  ' + file : '')}));
    if (e.Notes) meta.append(el('br'), el('i', {textContent: e.Notes}));

    const card = el('div', {className: 'entry' + (i === selected ? ' selected' : '') + (file ? ' paired' : ''),
                            tabIndex: 0}, drop, meta);
    card.onclick = () => { selected = i; renderEntries(); };
    dropZone(card, d => assign(i, d.file, d.fromIdx));
    box.append(card);
  });
}

function renderPool() {
  const pool = $('pool'); pool.replaceChildren();
  const hide = $('hide-used').checked;
  const order = poolOrder();
  const visible = order.filter(f => !(hide && fileOwner(f) !== null));
  $('pool-count').textContent = `(${visible.length} shown / ${images.length})`;
  for (const f of visible) {
    const owner = fileOwner(f), info = infoByName[f] || {};
    const showDate = $('sort').value.startsWith('exif') ? (info.exifDate || 'no EXIF date')
                                                        : fmtTime(info.mtime);
    const label = el('div', {textContent: f}, el('div', {className: 'sub', textContent: showDate}));
    if (owner !== null) label.append(el('div', {className: 'badge',
      textContent: '→ #' + (entries[owner].ImageNumber ?? owner + 1)}));
    const t = el('div', {className: 'thumb' + (owner !== null ? ' used' : ''), draggable: true, tabIndex: 0,
      title: [f, info.w && `${info.w}×${info.h}`, info.size && fmtSize(info.size),
              'Modified: ' + fmtTime(info.mtime), info.exifDate && 'EXIF: ' + info.exifDate]
             .filter(Boolean).join('\n')},
      makeImg(f, visible), label);
    t.addEventListener('dragstart', e => setDragData(e, f, owner));
    t.onclick = () => {
      if (selected === null) return;
      const target = selected;
      assign(target, f);
      select(nextUnpaired(target + 1) ?? target);
    };
    t.onkeydown = ev => { if (ev.key === 'Enter') t.onclick(); };
    pool.append(t);
  }
}

function render() {
  renderEntries(); renderPool();
  const n = Object.keys(pairs).length;
  const s = `${n}/${entries.length} entries paired · ${images.length} images`;
  $('status').dataset.base = s; $('status').textContent = s;
}

function showLightbox(file, gallery) {
  lbFiles = gallery && gallery.length ? gallery : [file];
  lbPos = Math.max(0, lbFiles.indexOf(file));
  updateLightbox(); $('lightbox').showModal();
}
function updateLightbox() {
  const f = lbFiles[lbPos], o = fileOwner(f);
  $('lightbox').querySelector('img').src = imgUrl(f);
  $('lb-cap').textContent = `${f}  (${lbPos + 1}/${lbFiles.length})` +
    (o !== null ? `  → #${entries[o].ImageNumber ?? o + 1}` : '');
}

// ---------------------------------------------------------------- keyboard
document.addEventListener('keydown', e => {
  const lb = $('lightbox');
  if (lb.open) {
    if (e.key === 'ArrowRight') { lbPos = (lbPos + 1) % lbFiles.length; updateLightbox(); }
    if (e.key === 'ArrowLeft') { lbPos = (lbPos - 1 + lbFiles.length) % lbFiles.length; updateLightbox(); }
    return;
  }
  if ($('write-dlg').open || ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); undo(); return; }
  if (!entries.length) return;
  if (e.key === 'ArrowDown') { e.preventDefault(); select(Math.min((selected ?? -1) + 1, entries.length - 1)); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); select(Math.max((selected ?? 1) - 1, 0)); }
  else if ((e.key === 'Delete' || e.key === 'Backspace') && selected !== null) { e.preventDefault(); unassignIdx(selected); }
  else if (e.key.toLowerCase() === 'n') jumpUnpaired();
});


// ---------------------------------------------------------------- export / write
async function exportJson() {
  const r = await fetch('/api/export', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                        body: JSON.stringify({pairs})});
  const url = URL.createObjectURL(await r.blob());
  el('a', {href: url, download: 'paired.json'}).click();
  URL.revokeObjectURL(url);
}

function openWrite() {
  if (!Object.keys(pairs).length) return alert('Pair some images first.');
  const keys = [...new Set(entries.flatMap(Object.keys))].filter(k => k !== 'SourceFile');
  $('tags').replaceChildren(...keys.map(k =>
    el('label', {}, el('input', {type: 'checkbox', value: k, checked: true}), ' ' + k)));
  if (!$('out-dir').value) $('out-dir').value = imgDir.replace(/\/$/, '') + '/tagged';
  const n = Object.keys(pairs).length;
  $('write-summary').textContent = `${n} paired image(s) will be written; ${entries.length - n} unpaired entries are skipped.`;
  $('write-out').hidden = true;
  $('write-dlg').showModal();
}
function setAllTags(v) { document.querySelectorAll('#tags input').forEach(c => c.checked = v); }

async function doWrite() {
  const mode = document.querySelector('input[name=mode]:checked').value;
  if (mode === 'inplace' && !confirm('This modifies your original files. Continue?')) return;
  const body = {
    pairs, mode, outDir: $('out-dir').value, rename: $('rename').value,
    backup: $('backup').checked, createDate: $('create-date').checked,
    tags: [...document.querySelectorAll('#tags input:checked')].map(c => c.value),
  };
  $('write-btn').disabled = true;
  const out = $('write-out'); out.hidden = false; out.className = ''; out.textContent = 'Writing…';
  try {
    const d = await api('/api/write', body);
    out.className = d.ok ? '' : 'err';
    out.textContent = (d.outDir ? 'Output: ' + d.outDir + '\n' : '') + d.output;
  } catch (e) { out.className = 'err'; out.textContent = e.message; }
  finally { $('write-btn').disabled = false; }
}

// ---------------------------------------------------------------- init
dropZone($('pool'), d => unassignFile(d.file));
(async () => {
  const d = await (await fetch('/api/state')).json();
  loadPrefs();
  entries = d.entries; setImages(d.images); imgDir = d.dir; jsonName = d.name;
  $('img-dir').value = imgDir;
  if (jsonName) $('json-name').textContent = '— ' + jsonName;
  restorePairs(); render();
  if (!d.exiftool) $('status').textContent = $('status').dataset.base = 'exiftool not found — writing disabled';
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    # Bound to localhost only: there is no authentication.
    app.run(host="127.0.0.1", port=5000, debug=False)
