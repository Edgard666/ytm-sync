#!/bin/sh
# Start the controller as an unprivileged user (Unraid default: nobody:users).
# Root is only used, if needed, to hand /data over to that user, then dropped.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
DATA_DIR="${DATA_DIR:-/data}"

case "$PUID$PGID" in
  *[!0-9]*|'') echo "PUID et PGID doivent être numériques" >&2; exit 1 ;;
esac

# JOAL .client profiles shipped in the image: add the missing ones to $DATA_DIR/clients.
# A profile already there (even edited by the user) is never replaced or removed.
sync_clients() {
  [ -d /opt/joal/clients ] || return 0
  mkdir -p "$DATA_DIR/clients" 2>/dev/null || return 0
  added=0
  for profile in /opt/joal/clients/*.client; do
    [ -f "$profile" ] || continue
    target="$DATA_DIR/clients/${profile##*/}"
    [ -e "$target" ] && continue
    if cp "$profile" "$target" 2>/dev/null; then
      [ "$(id -u)" = "0" ] && chown "$PUID:$PGID" "$target" 2>/dev/null
      added=$((added + 1))
    fi
  done
  [ "$added" -gt 0 ] && echo "Profils JOAL ajoutés dans $DATA_DIR/clients : $added"
  return 0
}

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA_DIR"
  sync_clients
  # Upgrade from v1.22 (files created by root): fix ownership once.
  if find "$DATA_DIR" -xdev \( ! -user "$PUID" -o ! -group "$PGID" \) -print 2>/dev/null | head -n 1 | grep -q .; then
    echo "Attribution de $DATA_DIR à $PUID:$PGID"
    chown -R "$PUID:$PGID" "$DATA_DIR" || echo "AVERTISSEMENT : chown partiel sur $DATA_DIR" >&2
  fi
  export HOME=/tmp
  # Probe first: without SETUID/SETGID capabilities the drop fails, and an
  # older template must not leave the container in a restart loop.
  if setpriv --reuid="$PUID" --regid="$PGID" --clear-groups true 2>/dev/null; then
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
  fi
  echo "AVERTISSEMENT : capacités SETUID/SETGID absentes, exécution en root." >&2
  echo "AVERTISSEMENT : lancer unraid/update-joal.sh puis Docker > Edit > Apply." >&2
  exec "$@"
fi

# Already started with --user: nothing to drop.
sync_clients
exec "$@"
