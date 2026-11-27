#!/bin/sh
# YTM Sync : demarre l'application avec l'utilisateur PUID:PGID (99:100 = nobody:users sur Unraid)
set -e
PUID="${PUID:-99}"
PGID="${PGID:-100}"
umask 077
mkdir -p /config/logs
if [ "$(id -u)" = "0" ]; then
  # reprend la propriete des fichiers crees par les versions precedentes (lancees en root),
  # seulement si un fichier n'appartient pas deja a PUID:PGID (evite de tout parcourir a chaque demarrage)
  if [ -n "$(find /config \( ! -user "$PUID" -o ! -group "$PGID" \) -print -quit)" ]; then
    chown -R "$PUID:$PGID" /config
  fi
  chmod 700 /config
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups env HOME=/config python -m app.main
fi
exec python -m app.main
