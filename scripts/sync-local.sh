#!/usr/bin/env bash
# Brings a local checkout in line with origin/main: dependencies, node_modules,
# and the dev database schema.
#
# Read-only about your work by default. It refuses to move a dirty tree and
# never switches branches on its own; pass --reset to fast-forward main.
#
# The install commands mirror .github/workflows/python-ci.yml deliberately. If
# CI's change, change these with them -- a local environment that installs
# differently from CI is the thing this script exists to prevent.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

RESET=0
SKIP_DB=0
for arg in "$@"; do
    case "$arg" in
        --reset) RESET=1 ;;
        --skip-db) SKIP_DB=1 ;;
        -h|--help)
            echo "usage: scripts/sync-local.sh [--reset] [--skip-db]"
            echo "  --reset    fast-forward main to origin/main (refuses if dirty)"
            echo "  --skip-db  do not touch the dev database"
            exit 0
            ;;
        *) echo "FAIL: unknown argument '$arg'" >&2; exit 2 ;;
    esac
done

step() { printf '\n=== %s ===\n' "$1"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# --- git -------------------------------------------------------------------

step "git"
git fetch --prune origin

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
DIRTY="$(git status --porcelain)"

if [ "$RESET" = "1" ]; then
    [ -n "$DIRTY" ] && fail "working tree is dirty; commit or stash before --reset"
    [ "$BRANCH" != "main" ] && fail "on '$BRANCH', not main; switch first (this script will not do it for you)"
    git merge --ff-only origin/main
elif [ "$BRANCH" = "main" ] && [ -n "$(git rev-list HEAD..origin/main)" ]; then
    echo "NOTE: main is $(git rev-list --count HEAD..origin/main) commit(s) behind origin/main. Re-run with --reset."
fi

# Branching from a local branch that was squash-merged leaves a commit whose
# content is already upstream. The PR then conflicts, and because GitHub cannot
# build a merge ref for a conflicting PR, every pull_request-triggered check
# silently never runs -- it reads as slow CI, not as a broken branch.
if [ "$BRANCH" != "main" ]; then
    BASE="$(git merge-base HEAD origin/main)"
    if [ "$BASE" != "$(git rev-parse origin/main)" ]; then
        echo "NOTE: '$BRANCH' is not based on current origin/main."
        echo "      Rebase before opening a PR:  git rebase origin/main"
    fi
fi

GONE="$(git branch -vv | awk '/: gone]/{print $1}' || true)"
[ -n "$GONE" ] && echo "NOTE: branches whose remote is gone:" && echo "$GONE"

# --- python ----------------------------------------------------------------

step "python dependencies"
command -v uv >/dev/null || fail "uv not on PATH"
[ -x .venv/bin/python ] || fail ".venv missing; create it before running this"

# --no-deps is deliberate: the lock is already the full closure, and letting the
# resolver run again at install time is what reintroduced nondeterminism.
VIRTUAL_ENV="$REPO_ROOT/.venv" uv pip install --no-deps --require-hashes \
    -r requirements-lock.txt
VIRTUAL_ENV="$REPO_ROOT/.venv" uv pip install --no-deps -e .
VIRTUAL_ENV="$REPO_ROOT/.venv" uv pip install --constraint requirements-lock.txt \
    pytest==9.1.1 pytest-mock factory-boy faker easydict devtools schemathesis coverage

# Catches a runtime dependency that is imported but not declared: it resolves
# from some other package's tree until that package moves, then the image dies
# at import with every gate still green.
step "import check"
DATABASE_HOSTNAME=localhost DATABASE_NAME=unused \
    DATABASE_CREDENTIALS=u:p DISPATCH_ENCRYPTION_KEY=x DISPATCH_JWT_SECRET=x \
    DISPATCH_UI_URL=http://localhost \
    .venv/bin/python -c 'import dispatch.main' >/dev/null \
    && echo "dispatch.main imports"

# --- frontend --------------------------------------------------------------

# ci, never install: only ci detects a package.json/lockfile desync. install
# silently repairs it, which has left main unable to build the image while every
# local check passed. A stale node_modules also lints Vue 3 against Vue 2 rules
# without erroring.
step "node_modules"
for dir in . docs src/dispatch/static/dispatch; do
    [ -f "$dir/package-lock.json" ] || continue
    echo "-- $dir"
    (cd "$dir" && npm ci --ignore-scripts --no-audit --no-fund >/dev/null)
done
echo "three lockfiles installed clean"

# --- dev database ----------------------------------------------------------

if [ "$SKIP_DB" = "0" ]; then
    step "dev database"
    [ -f docker/.env ] || fail "docker/.env missing; run docker/dev-setup.sh"

    set -a
    # shellcheck disable=SC1091
    . docker/.env
    set +a

    # `database upgrade` does NOT prompt -- it reads DATABASE_NAME straight from
    # the environment and ignores anything piped at it. Sourcing docker/.env
    # above sets DATABASE_NAME=dispatch, so this must be set explicitly or the
    # target is whatever happened to be in scope.
    export DATABASE_HOSTNAME=localhost
    export DATABASE_NAME=dispatch

    docker exec docker-postgres-1 pg_isready -q -h 127.0.0.1 \
        || fail "docker-postgres-1 is not accepting TCP connections; docker compose -f docker/docker-compose.yml up -d"

    echo "upgrading '$DATABASE_NAME' on docker-postgres-1"
    .venv/bin/python -m dispatch.cli database upgrade
fi

# --- verify ----------------------------------------------------------------

step "done"
cat <<'EOF'
Verify with:
  set -a; . docker/.env; set +a
  DATABASE_HOSTNAME=localhost .venv/bin/python -m pytest -q

pytest uses its own `dispatch-test` database, so a stale dev database does not
show up there -- it shows up when you run the app.
EOF
