# Contributing to Dispatch

This is a single-maintainer, best-effort project — an independently
maintained continuation of Netflix's archived `dispatch`. Contributions are
welcome; response time is not guaranteed.

## Before you start

- **This repo is the application** (API, frontend, plugins, Alembic
  migrations, `docker/Dockerfile`). Changes to Compose, `install.sh`, or
  deployment CI belong in
  [`Jamyn/dispatch-docker`](https://github.com/Jamyn/dispatch-docker)
  instead.
- For anything non-trivial, open an issue first to discuss the approach
  before writing code.
- Security vulnerabilities should **not** be filed as public issues — see
  [`SECURITY.md`](SECURITY.md).

## Development setup

```bash
uv sync --extra dev          # Python deps, including pytest/ruff/pre-commit (see pyproject.toml / requirements-lock.txt)
cd src/dispatch/static/dispatch && npm ci   # frontend deps
```

`docker/dev-setup.sh` bootstraps a local Postgres and generates
`docker/.env`. See the repository's project documentation for the full local
validation workflow (running the API, frontend dev server, and end-to-end
tests against a sample-data-seeded database).

## Making changes

- Keep pull requests focused — one logical change per PR.
- Match existing code style; there's no separate style guide beyond what the
  linters (`ruff`, `eslint`, `prettier`) enforce.
- Add or update tests for behavior you change. CI runs ruff, pytest
  (against Postgres, on Python 3.14), eslint, and vitest on every PR, and
  builds the docs site when `docs/` changes. Playwright e2e is local-only —
  run it before opening a PR that touches user-facing flows.
- Don't regenerate `src/dispatch/static/dispatch/components.d.ts` as part of
  an unrelated change; a production frontend build rewrites it as a side
  effect.

## Commit messages

Use a conventional prefix: `feat`, `fix`, `docs`, `refactor`, `test`,
`build`, `ci`, `chore`, `perf`, or `security`. Lowercase, short and factual:

```
fix: correct signature verification for slack event payloads
```

**Every commit must be signed.** GitHub will reject unsigned commits on
`main` — see the required-signatures rule below.

## Pull requests

- Target `main`.
- Every commit must be signed (`required_signatures` is enforced on `main`);
  unsigned commits, including those from automation, cannot be merged.
- Apply at least one primary label: `bug`, `enhancement`, `documentation`,
  `maintenance`, `security`, `ci`, or `breaking-change` — this is required
  by `enforce-labels` and also drives the categorized release notes. Topic
  labels (`postgres`, `docker`, etc.) are optional.
- `lockfile-sync` and `python-lock-sync` are required checks. **A bare-host
  `npm ci --dry-run` is not sufficient evidence the frontend lockfile is in
  sync** — npm's peer-dependency resolution is sensitive to local npm cache
  state, and a warm cache can silently mask packages (e.g.
  `eslint-plugin-vuetify`'s nested `@typescript-eslint@8.67.0` tree) that a
  clean environment, and CI, require. If you change frontend dependencies,
  regenerate and validate `package-lock.json` inside a container matching
  CI's `node:22` (floating, not pinned to a patch — CI floats too):
  ```bash
  cd src/dispatch/static/dispatch
  docker run --rm -v "$PWD:/w" -w /w node:22 \
    npm install --package-lock-only --ignore-scripts --no-audit --no-fund
  docker run --rm -v "$PWD:/w" -w /w node:22 \
    npm ci --dry-run --ignore-scripts --no-audit --no-fund   # must pass clean
  ```
  For `requirements-lock.txt`, regenerate with the command in its own header
  comment — the Python lock is already compiled inside
  `python:3.14.0-slim-trixie` for exactly this reason.
- No approvals are required to merge, but all required checks must pass and
  all commits must be signed.

## Reporting issues

Bug reports and feature requests for the application belong in this repo's
issue tracker. Deployment/Compose/installer issues belong in
[`Jamyn/dispatch-docker`](https://github.com/Jamyn/dispatch-docker/issues).
Suspected vulnerabilities should go through
[GitHub security advisories](https://github.com/Jamyn/dispatch/security/advisories/new)
instead of a public issue.

## Code of Conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). By
participating, you're expected to uphold it.
