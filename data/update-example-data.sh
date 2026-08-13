#!/usr/bin/env bash
# Regenerates dispatch-sample-data.dump against the local dev stack.
#
# Configuration comes from docker/.env rather than a committed .env beside this
# script: the dev database password is generated, so a second copy of these
# values would drift the first time it rotates. Exported variables outrank a
# .env file in starlette's Config resolution, so no .env is needed here.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_ENV="${SCRIPT_DIR}/../docker/.env"

if [ ! -f "$DEV_ENV" ]; then
    echo "FAIL: ${DEV_ENV} not found. Run docker/dev-setup.sh first."
    exit 1
fi

set -a
# shellcheck disable=SC1090
. "$DEV_ENV"
set +a

# A .env that exists but was never filled in would otherwise surface as a
# ValueError from config.py splitting the placeholder on ':'.
if [ -z "${DATABASE_CREDENTIALS:-}" ] \
    || [ "$DATABASE_CREDENTIALS" == "REPLACE_WITH_GENERATED_SECRET" ]; then
    echo "FAIL: DATABASE_CREDENTIALS is not set in ${DEV_ENV}. Run docker/dev-setup.sh first."
    exit 1
fi

# The dump paths below are relative to this directory.
cd "$SCRIPT_DIR"

echo "Dropping existing database..."
dispatch database drop
echo "Restoring current dump file..."
dispatch database restore --dump-file ./dispatch-sample-data.dump
echo "Running database migrations..."
dispatch database upgrade
echo "Dumping sql to file..."
dispatch database dump --dump-file ./dispatch-sample-data.dump

# pg_dump 18 brackets its output in \restrict/\unrestrict psql meta-commands
# keyed on a token it regenerates every run. Keeping them would churn the
# committed file on every regeneration and restrict the dump to psql, which is
# not the only thing that loads it.
grep -v '^\\restrict \|^\\unrestrict ' ./dispatch-sample-data.dump > ./.dump.tmp
mv ./.dump.tmp ./dispatch-sample-data.dump

# This script restores, upgrades and re-dumps, so a schema the upgrade cannot
# repair is round-tripped back out rather than fixed (issue #90). Verify the
# file that was just written instead of trusting the round trip.
echo "Verifying the regenerated dump against the models..."
cd "${SCRIPT_DIR}/.."
python -m pytest tests/database/test_sample_data.py -q
