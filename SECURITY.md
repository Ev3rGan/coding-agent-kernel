# Security Policy

## Supported versions

Coding Agent Kernel is experimental `0.x` software. Security fixes are made on the current `main`
branch and may be included in the current `0.x` release line; older snapshots are not guaranteed
backports.

| Version | Receiving security updates |
| --- | --- |
| Current `0.x` / `main` | Yes |
| Older snapshots | No guaranteed backports |

This support policy means reports are accepted and fixes are considered. It does not create a
response-time or remediation SLA.

## Report a vulnerability privately

Do not open a public Issue, discussion, or pull request for a suspected vulnerability. Use the
repository's GitHub **Report a vulnerability** form to create a private security report:

<https://github.com/Ev3rGan/coding-agent-kernel/security/advisories/new>

Include the affected commit or version, impact, minimal reproduction, relevant configuration, and
any suggested mitigation. Redact credentials, API keys, tokens, personal data, private repository
content, and paid-service account details. Never submit a live secret, even through the private
form; revoke and rotate any credential that may have been exposed.

Maintainers will review reports as availability permits, ask for clarification when needed, and
coordinate remediation and disclosure through the private GitHub Security Advisory. Response and
fix timing depends on severity, reproducibility, and maintainer availability; no fixed SLA is
promised.

## Security model and limitations

Permission Modes are Kernel authorization controls, not operating-system isolation.
`LocalCodingEnvironment` runs with the authority of its Host process and is not a security sandbox
for malicious repositories or commands. In `full` mode, the Kernel additionally skips its approval
and workspace-containment checks. `full` must be limited to explicitly trusted, disposable
environments and does not provide container, VM, account, network, or privilege isolation.

Use operating-system or container isolation, least-privilege credentials, restricted network
access, disposable workspaces, and independent review appropriate to the code being executed.
