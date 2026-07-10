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
  starts. Indexing (and the one-time ~1 GB model download for faces/search)
  happens in the background — you can browse while it works.

### Lighter install (optional)

Face grouping and semantic search need extra ~1 GB models and Microsoft's C++
Build Tools to install. If you don't want them — or setup reports it couldn't
build them — you can skip them entirely: open a terminal in this folder and run

```
setup.cmd -SkipML
```

Everything else (timeline, albums, map, trips, duplicates, HEIC & video
thumbnails) works exactly the same.

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
