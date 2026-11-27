# Contributing

Thanks for helping. Outside pull requests have already shipped keyframes and
speed curves, a sandbox security fix and real audio-duration probing; the
README credits each one.

## Set up

```bash
git clone https://github.com/DareDev256/fcp-mcp-server
cd fcp-mcp-server
pip install -e ".[dev,intelligence]"
```

ffmpeg is needed for the preview and media tests (`brew install ffmpeg`).

## Before you open a pull request

```bash
ruff check . --exclude docs/
pytest tests/ -q
```

Both must pass; CI runs the same two commands.

- Add tests for what you change. A fix comes with a test that fails without it.
- Add a line under `## [Unreleased]` in CHANGELOG.md saying what changed and
  who it is for.
- Keep to the security invariants in [SECURITY.md](SECURITY.md): writes go
  through `_validate_output_path(anchor_dir=...)`, XML through
  `fcpxml/safe_xml.py`, subprocess calls in list form with timeouts.
- An existing operation does not change its output in a patch release. If
  yours must, say so in the pull request and it goes into the next minor.

## Reporting bugs and security issues

Bugs: open an issue with the FCPXML (or a trimmed copy) and what you expected.
Security issues: report privately, never in a public issue, as described in
[SECURITY.md](SECURITY.md).

## How decisions are made

See [GOVERNANCE.md](GOVERNANCE.md). Everyone follows the
[Code of Conduct](CODE_OF_CONDUCT.md).
