# Governance

fcp-mcp-server is maintained by one person, James Olusoga
([@DareDev256](https://github.com/DareDev256)), who has final say on what
merges and what ships. This file says how that works, so contributors and
users know what to expect.

## Roles

- **Maintainer:** @DareDev256. Reviews and merges pull requests, cuts
  releases, triages issues, and handles security reports.
- **Contributors:** anyone whose pull request has merged. Each one is credited
  in the CHANGELOG entry for the release that ships it.

A contributor who has landed several reviewed changes can be invited as a
second maintainer. That invitation, and any change to this file, is announced
in the CHANGELOG.

## How decisions are made

- Changes arrive as pull requests against `main`. CI (ruff and the full pytest
  suite on Python 3.10 to 3.12) must pass before merge.
- An operation that already exists does not change its output in a patch
  release. Behaviour changes take a minor version and are called out in the
  CHANGELOG under Changed.
- Every write the server makes goes through `_validate_output_path`, every XML
  parse goes through `fcpxml/safe_xml.py`, and every subprocess call uses list
  arguments with bounds and a timeout. A pull request that breaks one of these
  is not merged; see [SECURITY.md](SECURITY.md).

## Releases

Pushing a `v*` tag runs the Publish workflow: it tests, uploads to PyPI,
checks that PyPI serves the new version, and updates the official MCP
registry entry. The maintainer then installs the published package in a
clean environment before calling a release done. Version numbers follow
Semantic Versioning.

## Security

Vulnerabilities are reported privately as described in
[SECURITY.md](SECURITY.md). Fixes ship as a patch release with a published
GitHub security advisory that credits the reporter.

## Conduct

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).
