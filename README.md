# Simple Moozic Builder

A practical Project Zomboid pack builder for creating cassette/vinyl companion mods with real music tracks for **Build 42**.

This tool is designed to work with the **True MooZic** ecosystem and helps you generate workshop-ready music packs quickly, including:

- Cassette and vinyl item generation
- Optional A-side / B-side media pairing
- Built-in audio conversion workflow via bundled ffmpeg
- Cover workflow with poster override, defaults, and texture masking
- Build output preview and mod packaging utilities

## Why This Exists

Building music packs by hand is slow and error-prone. Simple Moozic Builder gives Zomboid modders a consistent workflow for creating child packs that plug into a base True MooZic parent mod.

## Features

- Build cassettes and vinyls from your song list
- B-side mapping support (where provided)
- Track-level art controls + mod-wide poster defaults
- Vinyl inner/outer mask generation
- Batch conversion status and build summaries
- Optional copy-to-workshop convenience flow

## Requirements

- Windows
- Project Zomboid Build 42 environment
- Parent mod installed if your generated pack uses a `require=` dependency (for example `\TrueMoozicTali`)

## Quick Start

1. Launch `SimpleMoozicBuilder.exe` from a release build, or run `simple_moozic_builder_ui.py` from source.
2. Set your audio/image source folders.
3. Add tracks and choose cassette/vinyl output per row.
4. Configure poster/default cover behavior.
5. Build and review generated output under `OUTPUT/`.
6. Publish generated pack to workshop from your normal mod upload workflow.

## Build From Source

```bat
cd "Simple Moozic Builder"
build_exe.bat
```

Output will be produced in:

- `dist/SimpleMoozicBuilder/`

## Repository Layout

- `simple_moozic_builder_ui.py` - main UI application
- `simple_moozic_builder.py` - build/export logic
- `assets/` - template and mask assets
- `build_exe.bat` - local Windows packaging helper

## Example Pack

An example generated pack can be published separately (for example, **TaliMix**) and linked back here as: “Built with Simple Moozic Builder”.

## Contributing

Contributions are welcome, but this is maintainer-led and optimized for practical modding workflows.

See `CONTRIBUTING.md`.

## License

MIT License. See `LICENSE`.