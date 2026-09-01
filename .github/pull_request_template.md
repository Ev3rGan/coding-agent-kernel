## Linked Issue

Link the originating Issue and any applicable approved specification. Do not use an automatic
closing keyword for a Parent Issue unless maintainers explicitly requested it.

## Scope and non-goals

Describe the smallest complete change and the adjacent behavior intentionally left unchanged.

## Validation

List exact commands and results. Distinguish full validation from skipped, platform-limited, or
partial checks.

- [ ] `python -m ruff check .`
- [ ] `python -m ruff format --check .`
- [ ] `python -m mypy`
- [ ] `python -m pytest`
- [ ] `python -m build`

## Security and secrets

Describe permission, path, subprocess, network, dependency, and data-handling impact. Confirm that
the diff and shared evidence contain no credentials, `.env` contents, private code, or
machine-specific paths.

## External runs and cost

State whether any real Provider, network, Docker/Harness, or other potentially paid run was
performed. If yes, record who authorized the exact run and provide sanitized, reproducible
evidence without secrets. If no, write `Not run`.

## Checklist

- [ ] The change is linked to an agreed Issue and any applicable specification, and preserves its non-goals.
- [ ] The diff contains only related files and no generated build artifacts.
- [ ] Public behavior, failure behavior, and documentation are consistent.
- [ ] Tests cover the relevant public seam or the reason they do not is documented.
- [ ] No secret, credential, private data, or developer-specific absolute path is included.
- [ ] Breaking changes, migrations, external dependencies, and remaining risks are explicit.
- [ ] I have not assumed authority to merge, release, deploy, or change repository governance.
