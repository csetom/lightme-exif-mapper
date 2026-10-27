# Film Metadata Mapper

Local web app for pairing scanned film photos with entries of an exiftool-style
JSON (e.g. a Lightme Logbook export) and writing that metadata into the images —
either into tagged copies or in place.

## Requirements

- Python 3.10+
- [exiftool](https://exiftool.org/) on `PATH` (`sudo apt install libimage-exiftool-perl`)

## Setup

```bash
python3 -m venv virtualenv
virtualenv/bin/pip install -r requirements.txt
```

## Run

```bash
virtualenv/bin/python meta_mapper.py
```

Open <http://127.0.0.1:5000>. The image folder defaults to the directory you
start the app from; change it with **Scan folder**.

## Usage

1. Load the JSON (upload or path) and scan the image folder.
2. Pair images with entries: drag & drop, click (select entry → click image),
   or **Auto-pair** in the current sort/filter order.
3. **Write metadata…** — choose tags, then write to copies (optionally renamed
   by frame number) or modify originals (with optional `_original` backup).
4. Optionally **Download paired JSON**.

Pairings are kept in browser storage per JSON + folder.

Shortcuts: `↑/↓` select entry · `Del` unpair · `N` next unpaired ·
`Ctrl+Z` undo · `←/→` browse in the enlarged view.

## Notes

- Binds to `127.0.0.1` only and has no authentication — don't expose it.
- Non-browser formats (TIFF, DNG…) show a preview only if they embed one;
  writing works regardless.
