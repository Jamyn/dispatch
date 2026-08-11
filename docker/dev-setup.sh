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
    secret_name=$1
    secret_bytes=$2
    if [ -z "${!secret_name}" ] || [ "${!secret_name}" == "$PLACEHOLDER_SECRET" ]; then
        value="$(openssl rand -hex "$secret_bytes")"
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
# `|| true` is load-bearing: on a fresh volume cat exits non-zero and set -e
# would abort. Since postgres 18 the image keeps PG_VERSION under a
# major-version subdirectory, so both locations are checked.
PG_VOLUME="${COMPOSE_PROJECT_NAME}_postgres-data"
EXISTING_PG_DATA="$(docker run --rm -v "${PG_VOLUME}":/db busybox \
    sh -c 'cat /db/PG_VERSION 2>/dev/null; cat /db/*/docker/PG_VERSION 2>/dev/null' || true)"

fill_uninitialised_secret "DISPATCH_JWT_SECRET" 30
fill_uninitialised_secret "PGADMIN_DEFAULT_PASSWORD" 16

if [ -z "$EXISTING_PG_DATA" ]; then
    fill_uninitialised_secret "POSTGRES_PASSWORD" 24
    fill_uninitialised_secret "DISPATCH_ENCRYPTION_KEY" 30
    # The application reads DATABASE_CREDENTIALS; the postgres image reads the
    # user and password separately. Keep them in step.
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
