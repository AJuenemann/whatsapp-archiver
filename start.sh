#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

log() { printf '[start] %s\n' "$1"; }

if ! command -v docker >/dev/null 2>&1; then
    echo "[start] Docker is required. Install Docker Desktop (macOS/Windows) or Docker Engine + Compose (Linux), then retry." >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "[start] Docker Compose v2 is unavailable. Update Docker Desktop or install the Docker Compose plugin, then retry." >&2
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "[start] Docker is installed but its engine is not running. Start Docker Desktop/the Docker service, then retry." >&2
    exit 1
fi

project_name="${COMPOSE_PROJECT_NAME:-whatsapp-archiver}"
project_has_foreign_containers() {
    local container_id owner_dir
    for container_id in $(docker ps --all --quiet --filter "label=com.docker.compose.project=${project_name}"); do
        owner_dir="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$container_id" 2>/dev/null || true)"
        if [[ -n "$owner_dir" && "$owner_dir" != "$PROJECT_DIR" ]]; then
            return 0
        fi
    done
    return 1
}

if project_has_foreign_containers; then
    if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
        echo "[start] ERROR: Compose project '$project_name' belongs to another checkout. Choose a unique COMPOSE_PROJECT_NAME." >&2
        exit 1
    fi
    project_suffix="$(printf '%s' "$PROJECT_DIR" | cksum | awk '{print $1}')"
    project_name="whatsapp-archiver-${project_suffix}"
    log "A same-named checkout exists; isolating this stack as ${project_name}."
fi
export COMPOSE_PROJECT_NAME="$project_name"

if [[ -z "${VIEWER_PORT:-}" && -f "$PROJECT_DIR/.env" ]]; then
    port_from_env="$(grep -E '^VIEWER_PORT=' "$PROJECT_DIR/.env" | tail -n 1 | cut -d= -f2- || true)"
    [[ -n "$port_from_env" ]] && VIEWER_PORT="$port_from_env"
fi
VIEWER_PORT="${VIEWER_PORT:-8000}"
if [[ ! "$VIEWER_PORT" =~ ^[0-9]{1,5}$ ]] || (( 10#$VIEWER_PORT < 1 || 10#$VIEWER_PORT > 65535 )); then
    echo "[start] VIEWER_PORT must be a port number from 1 to 65535." >&2
    exit 1
fi
VIEWER_PORT="$((10#$VIEWER_PORT))"

if command -v id >/dev/null 2>&1; then
    export LOCAL_UID="$(id -u)"
    export LOCAL_GID="$(id -g)"
fi

port_pids() {
    command -v lsof >/dev/null 2>&1 && lsof -ti "tcp:${VIEWER_PORT}" -sTCP:LISTEN 2>/dev/null || true
}

is_docker_listener() {
    local pid process
    for pid in $(port_pids); do
        process="$(ps -o comm= -p "$pid" 2>/dev/null || true)"
        case "$process" in
            *docker*|*com.docker*) ;;
            *) return 1 ;;
        esac
    done
    return 0
}

log "Checking port ${VIEWER_PORT}..."
if [[ -n "$(port_pids)" ]]; then
    container_id="$(docker compose ps -q app 2>/dev/null || true)"
    if [[ -n "$container_id" ]] && [[ "$(docker inspect -f '{{.State.Running}}' "$container_id" 2>/dev/null || true)" == "true" ]]; then
        published_port="$(docker port "$container_id" 8000/tcp 2>/dev/null || true)"
        if [[ "$published_port" == *":${VIEWER_PORT}" ]]; then
            log "The running viewer from this project already owns the port. Reusing it."
        else
            echo "[start] ERROR: Port ${VIEWER_PORT} is held by another process. Set VIEWER_PORT in .env or the environment and retry." >&2
            exit 1
        fi
    elif is_docker_listener; then
        old_container="$(docker compose ps -a -q app 2>/dev/null || true)"
        if [[ -n "$old_container" ]]; then
            log "Removing this project's stopped container; no other Compose project will be touched."
            docker compose down --remove-orphans
        fi
        if [[ -n "$(port_pids)" ]]; then
            echo "[start] ERROR: Port ${VIEWER_PORT} is still held by Docker. No unrelated container was stopped; choose another VIEWER_PORT." >&2
            exit 1
        fi
    else
        echo "[start] ERROR: Port ${VIEWER_PORT} is in use by another process. It was not touched. Set VIEWER_PORT in .env or the environment." >&2
        if command -v lsof >/dev/null 2>&1; then lsof -i "tcp:${VIEWER_PORT}" -sTCP:LISTEN >&2 || true; fi
        exit 1
    fi
fi

log "Building and starting the archive..."
docker compose up -d --build --remove-orphans

container_id="$(docker compose ps -q app)"
if [[ -z "$container_id" ]]; then
    echo "[start] ERROR: Docker Compose did not create the app container. Check: docker compose logs app" >&2
    exit 1
fi

log "Waiting for the viewer healthcheck..."
attempt=0
while (( attempt < 60 )); do
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
    [[ "$health" == "healthy" ]] && break
    attempt=$((attempt + 1))
    sleep 2
done
if [[ "$health" != "healthy" ]]; then
    echo "[start] ERROR: Viewer did not become healthy. Check: docker compose logs --tail=80 app" >&2
    docker compose logs --tail=80 app >&2 || true
    exit 1
fi

VIEWER_URL="http://127.0.0.1:${VIEWER_PORT}"
log "Viewer is ready: ${VIEWER_URL}"
if command -v open >/dev/null 2>&1; then
    open "$VIEWER_URL"
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$VIEWER_URL" >/dev/null 2>&1 &
else
    log "Open this URL in a browser: ${VIEWER_URL}"
fi