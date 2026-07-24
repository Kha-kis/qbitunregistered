# Security policy

`qbitunregistered` can delete torrent data and files. Treat unexpected
destructive behavior, authentication leaks, path traversal, unsafe
cross-seeding decisions, and dry-run mutations as security issues.

## Supported versions

Security fixes are provided for the latest published minor release. Release
candidates are supported while they are the newest available version. Older
minor releases may receive a fix when a safe upgrade is not practical, but
that is not guaranteed.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability.

Use GitHub's private vulnerability reporting for this repository:

<https://github.com/Kha-kis/qbitunregistered/security/advisories/new>

Include the affected version, configuration relevant to the issue, expected
and observed behavior, and a minimal reproduction when possible. Remove API
keys, passwords, tracker passkeys, notification URLs, torrent names, and local
filesystem paths before submitting.

You should receive an acknowledgement within seven days. After triage, the
maintainer will coordinate remediation and disclosure through the private
advisory. Please do not disclose the issue publicly until a fix or mitigation
is available.
