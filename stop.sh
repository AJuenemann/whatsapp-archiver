#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

log() { printf '[stop] %s\n' "$1"; }

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "[stop] Docker Compose v2 is required to stop this project." >&2
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "[stop] Docker engine is not running. Start Docker Desktop/the Docker service, then retry." >&2
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
        echo "[stop] ERROR: Compose project '$project_name' belongs to another checkout; it was not stopped." >&2
        exit 1
    fi
    project_suffix="$(printf '%s' "$PROJECT_DIR" | cksum | awk '{print $1}')"
    project_name="whatsapp-archiver-${project_suffix}"
    if project_has_foreign_containers; then
        echo "[stop] ERROR: Project name '${project_name}' also belongs to another checkout; nothing was stopped." >&2
        exit 1
    fi
fi
export COMPOSE_PROJECT_NAME="$project_name"

log "Stopping only this project's Compose stack (archive data is preserved)..."
docker compose down --remove-orphans
log "Done."