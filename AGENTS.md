# Agent instructions (scope: this repository and all subdirectories)

## Repository identity

- Work only in the independent Git repository rooted at `D:\AgentDev\coding-agent-dev`.
- The canonical GitHub repository is `Ev3rGan/coding-agent-kernel`.
- The default branch is `main`.
- The canonical `origin` is `git@github.com:Ev3rGan/coding-agent-kernel.git`; keep Git operations on SSH rather than replacing it with an HTTPS remote.
- The user-level SSH configuration intentionally routes `github.com` through `ssh.github.com:443`. Preserve that external configuration and never store SSH private keys, tokens, device codes, or other credentials in this repository.

## Git and GitHub preflight

Before any Git or GitHub mutation:

1. Run `git rev-parse --show-toplevel` and require the exact repository root above. The parent directory `D:\AgentDev` is a separate Git repository and is outside this project's scope.
2. Run `git status --short --branch` and inspect all existing changes without assuming the worktree is clean.
3. Run `git remote -v` and confirm the canonical SSH remote before fetching, pushing, or publishing.
4. For GitHub-side changes, run `gh auth status` and confirm the active account is `Ev3rGan`.

The preflight is complete only when the physical root, current branch, existing diff, remote target, and active GitHub account are all known.

## Git workflow

- Preserve unrelated user changes and stage explicit paths only.
- Use `main` as the default comparison base unless the task names another fixed point.
- Treat commits, pushes, pull requests, releases, visibility changes, repository settings, branch deletion, history rewrites, and force pushes as explicit user-authorized actions.
- Prefer non-destructive, non-interactive Git commands. Never use `git reset --hard`, `git clean`, or force-push as a recovery shortcut.
- Keep secrets and machine-specific credentials out of commits and command output.

## Connection and verification

- Diagnose SSH with `ssh -T git@github.com`. A successful GitHub authentication prints `Hi Ev3rGan!` and normally exits with status `1` because GitHub does not provide shell access.
- Verify remote read access with `git ls-remote --heads origin main`.
- After local changes, run `git diff --check` and `git status --short --branch`.
- After a push, confirm the intended branch tracks its remote and that local `HEAD` matches the published commit.

## Repository guidance

- Derive the current project layout and commands from the worktree at task time; do not invent modules, entrypoints, or test commands that do not yet exist.
- Add nested `AGENTS.md` files only when a real component boundary introduces different commands, conventions, ownership, or risk.

## Agent skills

### Issue tracker

Issues and specs are tracked in GitHub Issues for `Ev3rGan/coding-agent-kernel`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the repository's canonical triage label vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository with a root `CONTEXT.md` and system-wide ADRs under `docs/adr/`. See `docs/agents/domain.md`.
