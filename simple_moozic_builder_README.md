`simple_moozic_builder.py` quickstart

Requirements:
- Python 3
- `Pillow` (`pip install pillow`)
- `CustomTkinter` for desktop UI (`pip install customtkinter`)
- `ffmpeg` in PATH for converting non-OGG audio

It builds child mods for True Moozic in one command.

Defaults in this folder:
- Audio input: `Put your audio here`
- OGG cache: `Put your audio here/_ogg`
- Images/posters: `Put your images here`
- Output: `OUTPUT`
- Assets: `assets`

Desktop UI:
```powershell
python .\simple_moozic_builder_ui.py
```

Buttons available in UI:
- Refresh Songs
- Convert Audio
- Rebuild all OGG files
- Build Pack

Cassette example:
```powershell
python .\simple_moozic_builder.py cassette `
  --mod-id TM_MyTapePack `
  --name "My Tape Pack" `
  --convert-audio
```

Vinyl example:
```powershell
python .\simple_moozic_builder.py vinyl `
  --mod-id TM_MyVinylPack `
  --name "My Vinyl Pack" `
  --cover ".\Put your images here\cover.png" `
  --convert-audio
```

Force rebuild all OGG cache files:
```powershell
python .\simple_moozic_builder.py vinyl --mod-id TM_MyVinylPack --force-rebuild-ogg
```

By default, output is written to:
- `.\OUTPUT\<mod-id>\Contents\mods\<mod-id>\...`

Optional:
- `--audio-dir` to override input folder
- `--out-dir` to override output folder
- `--assets-root` to override asset folder
- `--convert-audio` to run conversion before build
- `--force-rebuild-ogg` to rebuild every cached OGG

EXE packaging (Windows, one-folder)

1. Install build dependency:
```powershell
pip install pyinstaller
```

2. Build:
```powershell
.\build_exe.bat
```

Bundled converter (FFmpeg included in EXE distribution):
- Place ffmpeg at: `.\ffmpeg\ffmpeg.exe` before running `build_exe.bat`.
- The build script will copy it to: `dist\SimpleMoozicBuilder\ffmpeg\ffmpeg.exe`.
- At runtime, the app prefers bundled ffmpeg first, then falls back to system PATH.

3. Ship this folder to users:
- `dist\SimpleMoozicBuilder\`

Important:
- Do **not** run from `build\SimpleMoozicBuilder\` (that is temporary/intermediate output).
- Run and distribute only from `dist\SimpleMoozicBuilder\`.

Runtime behavior (important):
- On startup, the app auto-creates (next to the EXE):
  - `Put your audio here`
  - `Put your audio here\_ogg`
  - `Put your images here`
  - `OUTPUT`

Notes:
- This uses one-folder EXE mode so `assets/` is bundled predictably.
- Include FFmpeg license/readme files when distributing if required by your FFmpeg package.
