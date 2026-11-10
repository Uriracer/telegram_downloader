#!/usr/bin/env bash
# Telegram Downloader — install or update it in the media stack of the LXC container.
#
#   curl -fsSL https://raw.githubusercontent.com/Uriracer/telegram_downloader/main/deploy/install-lxc.sh | sudo bash
#   (or, from a copy of this file:  sudo bash install-lxc.sh)
#
# Private repository: the container needs read access. Either a deploy key
# (REPO_URL=git@github.com:Uriracer/telegram_downloader.git) or a fine-grained
# token with read-only "Contents" access (git asks for it once; use the token as password).
#
# What it does (it can be run again any time: it updates instead of duplicating):
#   1. checks that the NAS is mounted and creates the sentinel file /mnt/Plex/.tgdl-nas;
#   2. clones (or updates) the repository into /opt/media/src/telegram-downloader;
#   3. adds the "telegram-downloader" service to /opt/media/docker-compose.yml, once,
#      after a backup, and validates the result with "docker compose config"
#      (if it is not valid, the backup is restored);
#   4. builds the image and (re)starts only that service.
#
# Settings (environment variables):
#   COMPOSE_FILE=/opt/media/docker-compose.yml  NAS_DIR=/mnt/Plex
#   REPO_URL=https://github.com/Uriracer/telegram_downloader.git  BRANCH=main  PORT=8766
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-/opt/media/docker-compose.yml}"
NAS_DIR="${NAS_DIR:-/mnt/Plex}"
REPO_URL="${REPO_URL:-https://github.com/Uriracer/telegram_downloader.git}"
BRANCH="${BRANCH:-main}"
PORT="${PORT:-8766}"
SERVICE="telegram-downloader"

STACK_DIR="$(dirname "$COMPOSE_FILE")"
SRC_DIR="$STACK_DIR/src/$SERVICE"
CONFIG_DIR="$STACK_DIR/config/$SERVICE"
SENTINEL="$NAS_DIR/.tgdl-nas"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mATENCIÓ:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# ------------------------------------------------------------------ checks
[ "$(id -u)" = 0 ] || die "Executa'l com a root (sudo bash $0)."
[ -f "$COMPOSE_FILE" ] || die "No existeix $COMPOSE_FILE (variable COMPOSE_FILE)."
docker compose version >/dev/null 2>&1 || die "No es troba «docker compose»."

info "Comprovant el NAS ($NAS_DIR)"
if ! mountpoint -q "$NAS_DIR"; then
    die "$NAS_DIR no està muntat. Munta'l primer (mount -a) i torna-ho a provar: si no, tot s'escriuria al disc local."
fi
if [ ! -e "$SENTINEL" ]; then
    touch "$SENTINEL"
    info "Creat el fitxer sentinella $SENTINEL"
else
    info "El fitxer sentinella ja hi és"
fi

# ------------------------------------------------------------------ source
if ! command -v git >/dev/null 2>&1; then
    info "Instal·lant git"
    apt-get update -qq && apt-get install -y -qq git >/dev/null
fi
if [ -d "$SRC_DIR/.git" ]; then
    info "Actualitzant el codi ($SRC_DIR, branca $BRANCH)"
    git -C "$SRC_DIR" fetch --quiet origin "$BRANCH"
    git -C "$SRC_DIR" checkout --quiet "$BRANCH"
    git -C "$SRC_DIR" reset --quiet --hard "origin/$BRANCH"
else
    info "Baixant el codi a $SRC_DIR"
    mkdir -p "$(dirname "$SRC_DIR")"
    if ! git clone --quiet --branch "$BRANCH" "$REPO_URL" "$SRC_DIR"; then
        rm -rf "$SRC_DIR"
        die "No s'ha pogut baixar $REPO_URL. Si el repositori és privat, fes servir una clau de desplegament
       (REPO_URL=git@github.com:Uriracer/telegram_downloader.git) o un token de només lectura com a contrasenya."
    fi
fi
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC_DIR/tgdl/__init__.py")"
REVISION="$(git -C "$SRC_DIR" rev-parse --short HEAD)"
info "Versió $VERSION ($REVISION)"

# ------------------------------------------------------------------ compose
if grep -qE "^  ${SERVICE}:" "$COMPOSE_FILE"; then
    info "El servei $SERVICE ja és al $COMPOSE_FILE: no es toca"
else
    # The block is appended at the end, so "services:" must be the last top-level key.
    last_key="$(grep -E '^[A-Za-z_][A-Za-z0-9_-]*:' "$COMPOSE_FILE" | tail -n 1 | cut -d: -f1)"
    [ "$last_key" = "services" ] || die "L'última clau del fitxer és «$last_key», no «services»: afegeix el servei a mà (deploy/media-stack-snippet.yml)."
    if grep -qE '^x-env: *&env' "$COMPOSE_FILE"; then
        env_block='      <<: *env'
    else
        warn "No hi ha l'ancoratge «x-env: &env»: es posa PUID/PGID 1000 i TZ Europe/Madrid."
        env_block=$'      PUID: "1000"\n      PGID: "1000"\n      TZ: Europe/Madrid'
    fi

    backup="$COMPOSE_FILE.bak-$(date +%Y%m%d-%H%M%S)"
    cp -p "$COMPOSE_FILE" "$backup"
    info "Còpia de seguretat: $backup"
    [ -z "$(tail -c 1 "$COMPOSE_FILE")" ] || echo >> "$COMPOSE_FILE"
    cat >> "$COMPOSE_FILE" <<EOF

  ${SERVICE}:
    build: ./src/${SERVICE}
    image: ${SERVICE}:local
    container_name: ${SERVICE}
    environment:
${env_block}
      UMASK: "002"
      TGDL_DOWNLOAD_DIR: /data/Descarregues/telegram
      TGDL_LIBRARY_TV: /data/Series
      TGDL_LIBRARY_MOVIES: /data/Pelicules
      TGDL_STORAGE_SENTINEL: /data/.tgdl-nas
    volumes: [./config/${SERVICE}:/config, ${NAS_DIR}:/data]
    ports: ["${PORT}:8766"]
    restart: unless-stopped
    stop_grace_period: 30s
EOF
    if ! compose config -q; then
        cp -p "$backup" "$COMPOSE_FILE"
        die "El docker-compose.yml resultant no és vàlid; s'ha restaurat la còpia ($backup)."
    fi
    info "Servei afegit al $COMPOSE_FILE"
fi

mkdir -p "$CONFIG_DIR"

# ------------------------------------------------------------------ build and start
info "Construint la imatge (el primer cop triga uns minuts)"
compose build --pull \
    --build-arg VERSION="$VERSION" --build-arg REVISION="$REVISION" "$SERVICE"
info "Engegant $SERVICE"
compose up -d --no-deps "$SERVICE"

info "Esperant que respongui"
ok=""
for _ in $(seq 1 60); do
    if docker exec "$SERVICE" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/api/health', timeout=2)" >/dev/null 2>&1; then
        ok=1
        break
    fi
    sleep 2
done
if [ -z "$ok" ]; then
    compose logs --tail 40 "$SERVICE" || true
    die "No respon. Mira el registre de dalt (docker compose -f $COMPOSE_FILE logs $SERVICE)."
fi

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
docker image prune -f --filter "label=org.opencontainers.image.title=Telegram Downloader" >/dev/null 2>&1 || true
echo
info "Fet: Telegram Downloader $VERSION a http://${ip:-<ip-del-servidor>}:${PORT}/"
echo "    El primer cop: crea la contrasenya, posa l'api_id/api_hash i escaneja el QR."
echo "    Per actualitzar més endavant, torna a executar aquest mateix script."
