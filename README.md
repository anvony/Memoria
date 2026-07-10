# Memoria

*Your photos, on your machine.*

A local-first photo & video manager for Windows — like Google Photos, but
**nothing ever leaves your computer.** Memoria indexes the photo folders you
already have (including external drives), gives you a timeline, albums, map,
trips, duplicate detection, face grouping and "beach sunset" search — all
running offline on your own PC. Your original files are only ever **read**,
never modified, moved, or deleted.

---

## Get started (2 steps)

> **Requirements:** Windows 10/11 and **[Python 3.12](https://python.org)**
> (during install, tick **"Add Python to PATH"**). An internet connection is
> needed for the one-time setup below. Windows 11 already includes WebView2, so
> there's nothing else to install.

### 1. Run `setup.cmd` — once

Double-click **`setup.cmd`**. It builds Memoria's photo engine, downloads the
extra tools it needs, and puts a **Memoria** shortcut on your Desktop and Start
Menu. This takes a few minutes and only happens the first time.

### 2. Open **Memoria**

Launch it from the Desktop shortcut (or double-click **`Memoria.exe`** in this
folder). On first run Memoria asks:

1. **Where to keep its own data** — its catalogue and thumbnail cache. Pick a
   drive with some free space. *This is not your photo folder* — it's Memoria's
   private workspace, and it's the only thing you'd ever need to back up.
2. Then open **Settings → add a photo folder** by pasting its path, and watch it
   index. That's it.

---

## Good to know

- **Your originals are safe.** Memoria never edits, moves, or hard-deletes your
  files. Deletes go to the Recycle Bin; metadata edits live only in Memoria's
  own catalogue unless you explicitly opt in.
- **Everything is offline.** No account, no cloud, no telemetry. The only
  network use is downloading tools/models during setup.
- **Keep this folder together.** `Memoria.exe` and the `server` folder next to
  it are a pair — don't separate them, or the app can't find its engine. To move
  Memoria, move the whole folder and re-run `setup.cmd`.
- **First launch after setup** can take a few seconds while the photo engine
  starts. Indexing happens in the background — you can browse while it works.

### Faces & semantic search (optional — enable it yourself in Settings)

Face grouping and "beach sunset" semantic search are **off by default**, and
`setup.cmd` does **not** install them — that keeps the base install small and
fast.

To use them, you must **turn them on manually inside the app**:

1. Open Memoria → **Settings**.
2. Under **Faces & semantic search**, click **Turn on**.
3. The first time only, Memoria downloads everything it needs *on demand*
   (~2–3 GB of packages + models) with a progress bar — leave it running.
4. When it finishes, run a **rescan** so people and search results appear.

**Python is the only prerequisite** — no Visual Studio / C++ build tools, no
other software. Everything else (timeline, albums, map, trips, duplicates,
HEIC & video thumbnails) works fully without ever turning this on.

### The Windows warning

`Memoria.exe` is not code-signed, so Windows SmartScreen may say
"unrecognized app." Click **More info → Run anyway**. (Memoria is open source —
you can read exactly what it does in the `server` folder and the source repo.)

---

## Uninstall

Memoria isn't "installed" into Windows — it lives entirely in this folder.
To remove it:

1. Delete this folder.
2. Delete the shortcuts (Desktop + Start Menu).
3. Optionally delete Memoria's data folder (the one you chose at first launch)
   and `%LOCALAPPDATA%\Memoria`.

Your photos are untouched by any of this.
