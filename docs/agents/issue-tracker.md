# Issue tracker: GitHub

Issues and specs for this repository live in GitHub Issues under `Ev3rGan/coding-agent-kernel`. Use the `gh` CLI from the repository root for tracker operations.

Before publication, a user-approved frozen Spec under `docs/specs/` is the canonical planning source. Publishing a Parent mirrors the exact approved content into GitHub Issues; planning or publication must not silently rewrite the frozen local Spec.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`.
- **Read an issue**: `gh issue view <number> --comments` and include labels when the workflow depends on triage state.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments` with appropriate label and state filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`.
- **Apply or remove labels**: `gh issue edit <number> --add-label "..."` or `--remove-label "..."`.
- **Close an issue**: `gh issue close <number> --comment "..."`.

Infer the repository from the canonical SSH `origin`. Before any GitHub mutation, follow the identity and authentication preflight in the root `AGENTS.md`.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub shares one number space across issues and pull requests. Resolve an ambiguous reference with `gh pr view <number>` and fall back to `gh issue view <number>`.

## Skill terminology

When a skill says **publish to the issue tracker**, create a GitHub issue only after the user approves the exact content and publication action.

When a skill says **fetch the relevant ticket**, read the complete issue body, comments, labels, and dependency state.

## Parent and milestone issues

- Publish the approved Parent Spec first.
- Publish one issue per approved milestone in blocker-first order.
- Link every milestone to its Parent using GitHub sub-issues when available.
- Represent blocking with GitHub native issue dependencies when available. The dependency endpoint requires the blocker's numeric database ID, not its issue number or node ID.
- When native relationships are unavailable, put explicit `Parent: #<number>` and `Blocked by: #<number>` references in the milestone body.
- Do not close or rewrite a Parent as a side effect of publishing, implementing, or closing a milestone.
- Verify the Parent relation, milestone count, labels, and dependency graph after publication.
