# Security Policy

## Reporting a Vulnerability

Please report vulnerabilities privately, not via public issues or PRs.

**Preferred:** GitHub private vulnerability reporting. Go to the repo's
**Security** tab and click **Report a vulnerability**. This opens a private
advisory only the maintainer can see.

**Fallback:** email `dare@jamesdare.com` with subject `[SECURITY] fcp-mcp-server`.

You can expect an acknowledgment within 48 hours and a fix or mitigation plan
within 7 days for confirmed issues. Reporters are credited in the CHANGELOG
and release notes unless you ask otherwise.

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest release | Yes |
| Older releases | No, upgrade to latest |

## Threat Model Notes

This is an MCP server: tool arguments may be LLM-generated and indirectly
influenced by untrusted content (prompt injection via files a user asks an
assistant to process). Treat every path, XML document, and string argument as
adversarial. The defenses in place, and the invariants any change must
preserve, are documented in the [Security section of the README](README.md#security):

- All file writes must go through `_validate_output_path(anchor_dir=...)`.
- All XML parsing must go through `fcpxml/safe_xml.py` (defusedxml).
- All subprocess calls use list-form arguments, bounded parameters, and timeouts.

Reports that find a handler violating one of these invariants (as in the
`apply_template` sandbox gap fixed in v0.9.1) are exactly what this policy
is for.
