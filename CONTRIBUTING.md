# Contributing

Thanks for your interest.

This project is maintainer-led and focused on practical outcomes for Project Zomboid modding. PRs and issues are welcome.

## Ground Rules

- Keep changes scoped and practical.
- Prefer fixes that preserve existing builder workflows.
- Avoid adding heavy dependencies unless clearly justified.
- Match existing code style and naming conventions.

## Reporting Issues

Please include:

- What you expected
- What happened
- Steps to reproduce
- Relevant logs/screenshots
- Builder version / commit (if known)

## Pull Requests

- Open a focused PR with a clear title.
- Explain behavior changes and tradeoffs.
- Note any migration impact for existing projects.
- Keep UI changes consistent with current layout and intent.

## Local Validation

Before opening a PR:

1. Run `build_exe.bat` successfully.
2. Confirm app launch from `dist/SimpleMoozicBuilder/`.
3. Smoke-test: add tracks, convert, build output.

## Scope Note

The maintainer may decline broad refactors or changes that increase maintenance burden without clear value.