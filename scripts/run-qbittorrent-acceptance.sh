#!/usr/bin/env bash

set -euo pipefail

readonly QBITTORRENT_IMAGE="lscr.io/linuxserver/qbittorrent:5.2.3"
readonly QBITTORRENT_REPOSITORY="lscr.io/linuxserver/qbittorrent"
readonly QBITTORRENT_IMAGE_DIGEST="sha256:b024436f8ca665d16d9a997d26fd27fdf867ee5566ba09f32764e7b2976d3e02"
readonly CONTAINER_NAME="qbitunregistered-acceptance-$(date +%s)-$$"

container_started=false

fail() {
    echo "Acceptance test failed: $*" >&2
    exit 1
}

cleanup() {
    local exit_status=$?

    trap - EXIT
    if [[ "$container_started" == true ]] && docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
        if ((exit_status != 0)); then
            echo "qBittorrent logs omitted because they contain a temporary credential." >&2
        fi
        docker rm --force --volumes "$CONTAINER_NAME" >/dev/null 2>&1 || true
    fi
    unset QBITTORRENT_ACCEPTANCE_PASSWORD
    exit "$exit_status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

command -v docker >/dev/null 2>&1 || fail "docker is required"
command -v uv >/dev/null 2>&1 || fail "uv is required"
docker info >/dev/null 2>&1 || fail "the Docker daemon is unavailable"

if [[ -n "${QBITTORRENT_ACCEPTANCE_PORT:-}" ]]; then
    [[ "$QBITTORRENT_ACCEPTANCE_PORT" =~ ^[0-9]+$ ]] ||
        fail "QBITTORRENT_ACCEPTANCE_PORT must be an integer"
    ((QBITTORRENT_ACCEPTANCE_PORT >= 1024 && QBITTORRENT_ACCEPTANCE_PORT <= 65535)) ||
        fail "QBITTORRENT_ACCEPTANCE_PORT must be between 1024 and 65535"
    acceptance_port="$QBITTORRENT_ACCEPTANCE_PORT"
else
    acceptance_port="$(
        uv run --frozen python - <<'PY'
import socket

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
    )"
fi

echo "Pulling pinned qBittorrent acceptance image..."
docker pull "$QBITTORRENT_IMAGE"

expected_repo_digest="${QBITTORRENT_REPOSITORY}@${QBITTORRENT_IMAGE_DIGEST}"
repo_digests="$(docker image inspect "$QBITTORRENT_IMAGE" --format '{{range .RepoDigests}}{{println .}}{{end}}')"
if ! grep -Fxq "$expected_repo_digest" <<<"$repo_digests"; then
    fail "the pulled qBittorrent image does not match the expected digest"
fi

echo "Starting disposable qBittorrent ${QBITTORRENT_IMAGE##*:} on a loopback-only port..."
container_started=true
docker run \
    --detach \
    --name "$CONTAINER_NAME" \
    --env "WEBUI_PORT=$acceptance_port" \
    --publish "127.0.0.1:${acceptance_port}:${acceptance_port}/tcp" \
    "$QBITTORRENT_IMAGE" >/dev/null

temporary_password=""
for _ in {1..90}; do
    temporary_password="$(
        docker logs "$CONTAINER_NAME" 2>&1 |
            sed -n 's/.*temporary password is provided for this session: //p' |
            tail -n 1
    )"
    if [[ -n "$temporary_password" ]]; then
        break
    fi
    sleep 1
done
[[ -n "$temporary_password" ]] || fail "qBittorrent did not provide a temporary Web UI password"

export QBITTORRENT_ACCEPTANCE_HOST="127.0.0.1:${acceptance_port}"
export QBITTORRENT_ACCEPTANCE_PASSWORD="$temporary_password"
unset temporary_password

echo "Adding one synthetic torrent to the disposable qBittorrent instance..."
uv run --frozen python - <<'PY'
import os
import time

from qbittorrentapi import Client

info_hash = "0123456789abcdef0123456789abcdef01234567"
client = Client(
    host=os.environ["QBITTORRENT_ACCEPTANCE_HOST"],
    username="admin",
    password=os.environ["QBITTORRENT_ACCEPTANCE_PASSWORD"],
)
client.auth_log_in()
client.torrents_add(
    urls=f"magnet:?xt=urn:btih:{info_hash}&dn=qbitunregistered-acceptance"
)

for _ in range(30):
    torrents = client.torrents_info(torrent_hashes=info_hash)
    if torrents:
        break
    time.sleep(0.2)
else:
    raise SystemExit("Synthetic torrent did not appear in qBittorrent")

state = str(torrents[0].state)
if state.lower().startswith(("paused", "stopped")):
    raise SystemExit(f"Synthetic torrent unexpectedly started in state {state}")
print(f"qBittorrent {client.app.version} is ready with a synthetic torrent in state {state}")
PY

echo "Running qbitunregistered's pause operation in dry-run mode..."
uv run --frozen qbitunregistered \
    --config config.json.example \
    --host "$QBITTORRENT_ACCEPTANCE_HOST" \
    --username admin \
    --password "$QBITTORRENT_ACCEPTANCE_PASSWORD" \
    --pause-torrents \
    --dry-run \
    --yes

echo "Verifying that dry-run did not stop the synthetic torrent..."
uv run --frozen python - <<'PY'
import os

from qbittorrentapi import Client

info_hash = "0123456789abcdef0123456789abcdef01234567"
client = Client(
    host=os.environ["QBITTORRENT_ACCEPTANCE_HOST"],
    username="admin",
    password=os.environ["QBITTORRENT_ACCEPTANCE_PASSWORD"],
)
client.auth_log_in()
torrents = client.torrents_info(torrent_hashes=info_hash)
if len(torrents) != 1:
    raise SystemExit(f"Expected one synthetic torrent after dry-run, found {len(torrents)}")

state = str(torrents[0].state)
if state.lower().startswith(("paused", "stopped")):
    raise SystemExit(f"Dry-run mutated the synthetic torrent state to {state}")
print(f"Acceptance test passed; dry-run preserved the non-stopped torrent state ({state})")
PY
