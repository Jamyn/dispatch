# Security Policy

## Scope

This repository is the independently maintained continuation of the Dispatch application — the API, frontend, plugins, and `docker/Dockerfile` that [Jamyn/dispatch-docker](https://github.com/Jamyn/dispatch-docker) builds and runs. Reports in scope here are about the application itself: its code, its dependencies, and the container image it defines.

Vulnerabilities in the deployment tooling — `install.sh` (including secret generation and Postgres credential handling), `docker-compose.yml`, and that repository's CI — belong in [Jamyn/dispatch-docker](https://github.com/Jamyn/dispatch-docker) and are covered by [its security policy](https://github.com/Jamyn/dispatch-docker/blob/main/SECURITY.md).

[Netflix/dispatch](https://github.com/Netflix/dispatch), the archived upstream, was made read-only on 2025-09-01 and has no security response path; this repository is where application fixes land.

## Reporting a vulnerability

Report privately via GitHub: **[github.com/Jamyn/dispatch/security/advisories/new](https://github.com/Jamyn/dispatch/security/advisories/new)**. Please don't open a public issue for a suspected vulnerability. This is a single-maintainer, best-effort project with no SLA, but reports are read and acted on.

## Supported versions

Bug and security reports are accepted against the **latest tagged release** (`v*`, see [Releases](https://github.com/Jamyn/dispatch/releases)) and the current `main` tip only. Older releases receive no fixes; upgrade to the latest release before reporting. Fixes land on `main` and ship in the next release.
