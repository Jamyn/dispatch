# Dispatch

[![License](https://img.shields.io/github/license/Jamyn/dispatch)](LICENSE)
[![Release](https://img.shields.io/github/v/release/Jamyn/dispatch)](https://github.com/Jamyn/dispatch/releases)
[![Last commit](https://img.shields.io/github/last-commit/Jamyn/dispatch)](https://github.com/Jamyn/dispatch/commits/main)
[![Coverage](https://codecov.io/github/Jamyn/dispatch/badge.svg?branch=main)](https://codecov.io/github/Jamyn/dispatch)

### What's Dispatch?

Put simply, Dispatch is:

> All of the ad-hoc things you're doing to manage incidents today, done for you, and a bunch of other things you should've been doing, but have not had the time!

Dispatch helps effectively manage security incidents by deeply integrating with existing tools used throughout an organization (Slack, GSuite, Jira, etc.). Dispatch is able to leverage the existing familiarity of these tools to provide orchestration instead of introducing another tool.

This means you can let Dispatch focus on creating resources, assembling participants, sending out notifications, tracking tasks, and assisting with post-incident reviews; allowing you to focus on actually fixing the issue!

![Dispatch incident dashboard screenshot](https://github.com/Jamyn/dispatch/raw/main/docs/static/img/thumb-1.png) ![Dispatch incident detail screenshot](https://github.com/Jamyn/dispatch/raw/main/docs/static/img/thumb-2.png) ![Dispatch incident timeline screenshot](https://github.com/Jamyn/dispatch/raw/main/docs/static/img/thumb-3.png) ![Dispatch incident participants screenshot](https://github.com/Jamyn/dispatch/raw/main/docs/static/img/thumb-4.png) ![Dispatch incident tasks screenshot](https://github.com/Jamyn/dispatch/raw/main/docs/static/img/thumb-5.png) ![Dispatch incident report screenshot](https://github.com/Jamyn/dispatch/raw/main/docs/static/img/thumb-6.png)

## Status: independently maintained

[Netflix/dispatch](https://github.com/Netflix/dispatch) was archived and made read-only on **September 1, 2025**. This repository is a standalone continuation of that code, maintained independently of Netflix. It may diverge from upstream, including with breaking changes, to fix security issues, update outdated components, and adapt the application to our own use of the product.

Development happens on `main`, and releases are date-versioned tags (e.g. [`v26.08.12`](https://github.com/Jamyn/dispatch/releases/tag/v26.08.12)). Upstream's final commit is preserved as the [`upstream-final`](https://github.com/Jamyn/dispatch/releases/tag/upstream-final) tag if you need the pristine Netflix state.

The supported way to run Dispatch is [`Jamyn/dispatch-docker`](https://github.com/Jamyn/dispatch-docker), which builds this repository at a pinned release commit.

## Reporting issues

Bug reports and feature requests for the application belong in [this repo's issue tracker](https://github.com/Jamyn/dispatch/issues). Deployment, Compose, and installer issues belong in [`Jamyn/dispatch-docker`'s issue tracker](https://github.com/Jamyn/dispatch-docker/issues) instead. Suspected vulnerabilities in the application should be reported privately via [GitHub security advisories](https://github.com/Jamyn/dispatch/security/advisories/new) — please don't open a public issue for those.

Reports are accepted against the **latest tagged release** and the current `main` tip only.

## Project resources

- [Dispatch blog post](https://medium.com/@NetflixTechBlog/introducing-dispatch-da4b8a2a8072) (Netflix, 2020)
- [Documentation](https://jamyn.github.io/dispatch/) (built from this repository's `docs/`)
- [Docker deployment](https://github.com/Jamyn/dispatch-docker)
- [Upstream repository](https://github.com/Netflix/dispatch) (archived, read-only)
