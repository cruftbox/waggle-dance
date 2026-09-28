#!/bin/sh
# Update waggle-dance: pull the latest code, rebuild the image, restart the container.
# Run it from anywhere; it works in the directory it lives in.
set -e

cd "$(dirname "$0")"

if command -v docker >/dev/null 2>&1; then
    DOCKER="docker"
else
    # QNAP Container Station does not put docker on the SSH PATH.
    DOCKER="DOCKER_CONFIG=/tmp/.docker HOME=/tmp /share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker"
fi

git pull
eval "$DOCKER compose build"
eval "$DOCKER compose up -d"
