#!/usr/bin/env bash
# Generates development secrets into docker/.env and prints them.
#
# Mirrors dispatch-docker/install.sh's fill_uninitialised_secret: a value is
# generated only when it is empty or still the shipped sentinel, so re-running
# never invalidates a stack that is already up.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"

# Must match the placeholder in .env.example byte for byte, or every generation
# below silently no-ops and leaves the shipped value in place.
PLACEHOLDER_SECRET='REPLACE_WITH_GENERATED_SECRET'

if [[ "$OSTYPE" == "darwin"* ]]; then
    sed_suffix_arg="-i ''"
else
    sed_suffix_arg="-i"
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "Creating ${ENV_FILE} from .env.example..."
    cp "$ENV_EXAMPLE" "$ENV_FILE"
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

GENERATED=()

# printf -v, not declare: inside a function `declare NAME=` creates a local, so
# the assignment would not survive the return.
function fill_uninitialised_secret {
    local secret_name=$1
    local secret_bytes=$2
    local value
    if [ -z "${!secret_name}" ] || [ "${!secret_name}" == "$PLACEHOLDER_SECRET" ]; then
        value="$(openssl rand -hex "$secret_bytes")"
        # shellcheck disable=SC2086  # sed_suffix_arg must word-split for macOS -i ''
        sed $sed_suffix_arg "s|^${secret_name}=.*|${secret_name}=${value}|" "$ENV_FILE"
        printf -v "$secret_name" '%s' "$value"
        GENERATED+=("${secret_name}=${value}")
        echo "Generating ${secret_name}..."
    else
        echo "Leaving existing ${secret_name}..."
    fi
}

# Does the volume already hold a cluster? The postgres image applies
# POSTGRES_PASSWORD only during initdb, and DISPATCH_ENCRYPTION_KEY cannot be
# rotated without orphaning anything already encrypted -- so both gate on this.
# Since postgres 18 the image keeps PG_VERSION under a major-version
# subdirectory, so both locations are checked.
#
# The trailing `exit 0` is load-bearing: on a fresh volume both cats fail, and
# without it the probe's own failure would be indistinguishable from the
# daemon being unreachable. With it, a non-zero status means the probe could
# not run -- which must never be read as "no cluster", or a live cluster's
# password gets rotated out from under it.
#
# Note this creates the named volume if it does not exist yet, ahead of
# `docker compose`. Same name, same semantics, so compose adopts it.
PG_VOLUME="${COMPOSE_PROJECT_NAME}_postgres-data"
if ! EXISTING_PG_DATA="$(docker run --rm -v "${PG_VOLUME}":/db busybox \
    sh -c 'cat /db/PG_VERSION 2>/dev/null; cat /db/*/docker/PG_VERSION 2>/dev/null; exit 0')"; then
    echo "Could not inspect volume ${PG_VOLUME} -- is the docker daemon running?" >&2
    echo "Refusing to touch POSTGRES_PASSWORD or DISPATCH_ENCRYPTION_KEY without" >&2
    echo "knowing whether a cluster already exists there." >&2
    exit 1
fi

fill_uninitialised_secret "DISPATCH_JWT_SECRET" 30
fill_uninitialised_secret "PGADMIN_DEFAULT_PASSWORD" 16

if [ -z "$EXISTING_PG_DATA" ]; then
    fill_uninitialised_secret "POSTGRES_PASSWORD" 24
    fill_uninitialised_secret "DISPATCH_ENCRYPTION_KEY" 30
    # The application reads DATABASE_CREDENTIALS; the postgres image reads the
    # user and password separately. Keep them in step.
    # shellcheck disable=SC2086  # sed_suffix_arg must word-split for macOS -i ''
    sed $sed_suffix_arg \
        "s|^DATABASE_CREDENTIALS=.*|DATABASE_CREDENTIALS=${POSTGRES_USER}:${POSTGRES_PASSWORD}|" \
        "$ENV_FILE"
else
    echo "Existing Postgres cluster found in volume ${PG_VOLUME} (PG_VERSION ${EXISTING_PG_DATA})."
    echo "Leaving POSTGRES_PASSWORD and DISPATCH_ENCRYPTION_KEY untouched -- rotating"
    echo "either against existing data breaks authentication or orphans encrypted"
    echo "plugin configuration. Delete the volume to start over:"
    echo "    docker volume rm ${PG_VOLUME}"
fi

echo ""
if [ ${#GENERATED[@]} -eq 0 ]; then
    echo "Nothing generated; ${ENV_FILE} was already populated."
else
    echo "Generated secrets (written to ${ENV_FILE}):"
    printf '    %s\n' "${GENERATED[@]}"
fi

echo ""
echo "Start the dev stack:"
echo "    docker compose -f docker/docker-compose.yml up -d"
echo ""
echo "The application reads .env relative to the working directory, so load these"
echo "into your shell before running dispatch from the repository root:"
echo "    set -a; . docker/.env; set +a"
