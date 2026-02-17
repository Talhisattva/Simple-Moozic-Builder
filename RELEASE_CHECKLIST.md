# Release Checklist

## Pre-Release

- Update `README.md` if features changed
- Confirm `build_exe.bat` completes successfully
- Launch built app from `dist/SimpleMoozicBuilder/`
- Smoke-test key flows:
  - add tracks
  - convert audio
  - build mod output
  - poster/default cover behavior
  - B-side handling (if used)

## Versioning

- Update changelog/release notes draft
- Tag release commit (optional but recommended)

## GitHub Release

- Create new release on GitHub
- Upload zipped `dist/SimpleMoozicBuilder/` bundle
- Include:
  - changes summary
  - known limitations
  - upgrade notes (if any)

## Workshop / Ecosystem

- Rebuild example pack(s) if needed
- Verify mod dependency strings (`require=\...`) are correct
- Verify generated `workshop.txt` visibility is intended (`unlisted` by default)

## Post-Release

- Test fresh install path on a clean machine/profile
- Confirm links in workshop description point to latest release/repo