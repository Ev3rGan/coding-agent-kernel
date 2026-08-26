# Domain Docs

This is a single-context repository. Engineering skills consume the domain documentation using the rules below.

## Before exploring or planning

- Read the root `CONTEXT.md` for the project's canonical language.
- Read the ADRs under `docs/adr/` that affect the area being changed.
- Surface any conflict with an ADR instead of silently overriding it.

If a domain document does not exist, proceed silently. Create domain terms and ADRs only through an explicit domain-modeling workflow when a term or architectural decision has actually been resolved.

## Use the glossary vocabulary

Use the terms defined in `CONTEXT.md` in specs, issue titles, acceptance criteria, code-facing documentation, and architectural explanations. Avoid synonyms that the glossary marks as ambiguous.

If a required concept is missing, first decide whether it is genuinely specific to this Coding Agent Kernel. General programming concepts and implementation details do not belong in the glossary.

## ADR discipline

Create an ADR only when the decision is hard to reverse, surprising without its context, and the result of a real trade-off. Pi mechanisms inherited without a project-specific trade-off belong in the Spec and implementation references, not in one ADR per mechanism.
