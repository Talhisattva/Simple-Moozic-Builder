<img width="300" height="300" alt="SMBLOGO300" src="https://github.com/user-attachments/assets/336ac86c-2487-4efe-9e06-8355abb2d7e6" />
 
 ## Simple Moozic Builder

Simple Moozic Builder is a Windows tool. Makes mods for TrueMoozic! (and more)

Workflow: Track intake, conversion, cover processing, script generation, and workshop-ready output.

## What This Tool Is For

Use this when you want to build a companion mod that adds cassette and vinyl media items mapped to real songs.

Output includes:
- Cassette items
- Vinyl record + vinyl album/sleeve items
- Music definition Lua files
- Script/model/texture references
- `mod.info` and `workshop.txt` scaffolding
- no distrubtion - minimal asset packaging

## Requirements

- Windows
- Python 3.12+ (for running from source, exe self contained)
- Python dependencies listed in `requirements.txt`
- Project Zomboid Build 42 setup
- Parent mod installed if your pack depends on one (example: `\TrueMoozicTali`)
- Leaving this blank will package all dependant assets in the output

## Contributor Setup (One Command)

From repo root in PowerShell:

`.\setup_dev.ps1`

From repo root on Linux/macOS:

`./setup_dev.sh`

Optional (also install ffmpeg globally via winget):

`.\setup_dev.ps1 -InstallFfmpeg`

Then:

1. `.\.venv\Scripts\Activate.ps1`
2. `python .\simple_moozic_builder_ui.py`

## Quick Start

1. Start `simple_moozic_builder_ui.py` (or the packaged EXE from a release).
2. Set your audio source and image source folders.
3. Add songs to the table and choose Cassette/Vinyl per track.
4. Set cover behavior (poster override or mod-default behavior).
5. Build and inspect output.
6. Upload generated pack through your normal workshop flow.

## Main Workflow

### 1. Project Setup

Configure base metadata:
- Mod ID
- Mod Name
- Parent Mod dependency
- Author(s)
- Poster/preview image

### 2. Song Table

Manage tracks in one place:
- Add/remove songs
- Toggle cassette and vinyl output
- Optional B-side assignment
- Per-song cover assignment
- Per-row preview playback
- Convert status tracking

### 3. Cover Pipeline

Supports both quick and advanced cover flows:
- Poster override or Mod default (random base texture) for all covers
- Cassette/vinyl masking for all non-default covers 
- Vinyl inner/outer mask options
- High-resolution texture export (HR path support - Max 2048 x 2048) for inspecting album covers.

### 4. Build + Export

Build process generates a ready-to-publish mod folder structure and can copy output to workshop paths as configured.

## Key Features

- End-to-end music-pack generation
- Built-in ffmpeg conversion pipeline
- Experimental preview backend switch (miniaudio when installed, ffplay fallback)
- Experimental conversion backend switch via env var:
  - `SMB_AUDIO_BACKEND=auto` (default: prefer soundfile if installed, fallback ffmpeg)
  - `SMB_AUDIO_BACKEND=soundfile` (force soundfile path)
  - `SMB_AUDIO_BACKEND=ffmpeg` (force ffmpeg path)
- Mix/stitch creation follows the same backend policy:
  - `auto`: try soundfile first, fallback to ffmpeg
  - `soundfile`: soundfile only (fail fast if unavailable)
  - `ffmpeg`: ffmpeg only
- A-side / B-side media support - New Flip Feature in base mod
- Batch operations for cassette/vinyl toggles
- Save/load project state and recent files
- Mod metadata + dependency generation
- Make your own mix! Create Mix lets you generate a multi-song sound file on the go. 

## Hotkeys

- `Ctrl+S` Save project
- `Ctrl+A` Select all songs
- `Delete` Remove selected songs

## Repo Layout

- `simple_moozic_builder_ui.py` - UI and interaction logic
- `simple_moozic_builder.py` - build/export and generation logic
- `assets/` - masks, templates, and texture/model assets

## Example Usage in Workshop

You can publish a generated pack (for example, TaliMix) and reference this repo in its workshop description as the builder used to create it. Thank you :) 

## License

MIT. See `LICENSE`.
