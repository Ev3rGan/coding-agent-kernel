# Contributing

Thank you for helping improve Coding Agent Kernel. Keep each contribution focused, reviewable,
and grounded in an agreed Issue or specification.

## Start with an Issue and specification

Before substantial code, behavior, dependency, or governance changes, open an Issue that states
the problem, desired outcome, scope, non-goals, and acceptance evidence. Security reports are the
exception: follow [SECURITY.md](SECURITY.md) and do not open a public Issue.

The frozen Parent specifications under `docs/specs/` are canonical for their approved scope. If a
proposal conflicts with one, discuss and record an explicit specification amendment before
implementation. An implementation PR must link its Issue and preserve the agreed non-goals.

## Branches and pull requests

1. Start a focused branch from the current `main` branch in your fork or maintainer-authorized
   checkout.
2. Keep unrelated changes out of the branch and never commit credentials or machine-specific
   paths.
3. Open a pull request using the repository template. Describe the linked Issue, scope and
   non-goals, validation performed, and any external service, Docker, network, or paid run.
4. Address review findings with additional focused changes. Do not force-push or rewrite shared
   history without coordinating with the maintainers.

Opening a pull request does not authorize a merge, release, deployment, label change, repository
setting or ruleset change, branch deletion, or history rewrite. Those governance and publication
actions remain with maintainers and require their explicit decision.

## Development setup

Python 3.11 or newer is required. Create and activate a virtual environment, then install the
project and development tools from the repository root:

```console
python -m pip install -e ".[dev]"
```

The repository CI installs `.[dev,swebench]` to validate the complete optional integration
contract. Contributors may use `.[dev]` for ordinary Kernel work and add the pinned, heavier
official Harness dependency when validating SWE-bench integration changes.

## Validation

Run the deterministic local quality gates before requesting review:

```console
python -m ruff check .
python -m ruff format --check .
python -m mypy
python -m pytest
python -m build
```

The normal suite leaves `CODING_AGENT_RUN_NETWORK_TESTS` unset and does not require a real
Provider credential. Report skipped tests and platform limitations in the pull request rather
than presenting a partial run as full validation.

## Secrets, network access, and cost

- Keep API keys, tokens, `.env` files, credentials, artifacts containing secrets, and local
  workspace paths out of commits, Issues, pull requests, logs, and test fixtures.
- The CLI does not automatically load `.env`. A trusted Host may load a protected secret and
  inject it only into the child process that needs it.
- Do not run real Provider, external network, Docker/Harness, or other potentially paid tests
  unless the repository owner has explicitly authorized that exact run and its expected scope.
- Sanitize all evidence before sharing it. Never use a live secret as test data.

## Review, authorship, and signing

All changes require maintainer review; passing automation is necessary but not approval to merge.
Commit authorship must identify the actual contributor. If you add a `Signed-off-by` trailer, add
your own with `git commit -s` and never sign for someone else. Cryptographic commit signing and a
GitHub `Verified` badge are welcome, but neither replaces code review or grants publication
authority.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Contributions are
submitted under the repository's [Apache License 2.0](LICENSE) unless a separate written agreement
explicitly applies.
