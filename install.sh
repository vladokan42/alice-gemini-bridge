#!/bin/sh
# Bootstrap for a fresh supported server. Review locally before running as root.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if test "${1:-}" = --dry-run; then
    printf '%s\n' 'Dry-run: Debian 13 / Ubuntu 24.04; install Python/Git/OpenSSH/Go/GCC/Docker+Compose if missing, then download original stable releases and run server/configure/speaker wizard. No changes.'
    exit 0
fi
test "$(id -u)" = 0 || { printf '%s\n' 'Run with sudo on the server. Never pipe an unread remote script into sudo.' >&2; exit 1; }
. /etc/os-release
case "$ID:$VERSION_ID" in
    debian:13) compose_package=docker-compose;;
    ubuntu:24.04) compose_package=docker-compose-v2;;
    *) printf '%s\n' 'Supported bootstrap: Debian 13 or Ubuntu 24.04 only.' >&2; exit 1;;
esac
need=0
for command in python3 ssh git go gcc docker; do command -v "$command" >/dev/null 2>&1 || need=1; done
docker compose version >/dev/null 2>&1 || need=1
if test "$need" = 1; then
    printf '%s\n' 'Installing distribution packages: Python, SSH, Git, Go, GCC, Docker and Compose.'
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates python3 openssh-client git golang-go gcc libc6-dev docker.io "$compose_package"
fi
# Start Docker if necessary; do not restart a running daemon or change firewall.
if ! docker info >/dev/null 2>&1; then systemctl start docker; fi
if ! docker compose version >/dev/null 2>&1; then
    printf '%s\n' 'Docker Compose v2 missing. Use the supported distribution package; do not overwrite an existing Docker installation.' >&2
    exit 1
fi
exec python3 "$root/scripts/install.py" all "$@"
