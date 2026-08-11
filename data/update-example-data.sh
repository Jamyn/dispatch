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
