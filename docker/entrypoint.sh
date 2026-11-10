#!/bin/sh
# Telegram Downloader container entrypoint (based on 3Cat Downloader's).
#
# Runs the app as PUID:PGID (default 1000:1000) so the files it writes have the
# same owner as the rest of the media stack (Sonarr, Radarr, Jellyfin...).
# If the container is started with --user, it just runs as that user.
#
# The download folder is never created here: if the NAS is not mounted it
# would be created on the host's root disk. The app creates it only after
# checking the storage sentinel (TGDL_STORAGE_SENTINEL).
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-022}"
DATA_DIR="${TGDL_DATA_DIR:-/config}"
DOWNLOAD_DIR="${TGDL_DOWNLOAD_DIR:-/downloads}"
SENTINEL="${TGDL_STORAGE_SENTINEL:-}"

log() { printf '[entrypoint] %s\n' "$*" >&2; }

case "$PUID$PGID" in
    *[!0-9]*) log "PUID i PGID han de ser números (PUID=$PUID PGID=$PGID)."; exit 1 ;;
esac

umask "$UMASK"
export HOME="$DATA_DIR"

if [ "$(id -u)" != "0" ]; then
    exec tgdl "$@"
fi

if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd --non-unique --gid "$PGID" tgdl
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd --non-unique --uid "$PUID" --gid "$PGID" --home-dir "$DATA_DIR" \
        --no-create-home --shell /usr/sbin/nologin tgdl
fi

mkdir -p "$DATA_DIR"
# The config folder is small and ours: fix its ownership (it holds the Telegram session).
chown -R "$PUID:$PGID" "$DATA_DIR"
chmod 0700 "$DATA_DIR"

if [ -n "$SENTINEL" ] && [ ! -e "$SENTINEL" ]; then
    log "ATENCIÓ: no hi ha el fitxer sentinella $SENTINEL: el disc de xarxa no està muntat."
    log "L'aplicació arrenca igualment, però no escriurà res fins que hi sigui."
elif [ -d "$DOWNLOAD_DIR" ] && ! setpriv --reuid="$PUID" --regid="$PGID" --init-groups test -w "$DOWNLOAD_DIR"; then
    log "ATENCIÓ: l'usuari $PUID:$PGID no pot escriure a $DOWNLOAD_DIR."
    log "Revisa els permisos de la carpeta de l'amfitrió o ajusta PUID/PGID."
fi

log "Executant com a $PUID:$PGID (umask $UMASK)"
exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups tgdl "$@"
