#!/bin/bash
# =====================================================================
#  JOAL Smart Controller - mise à jour Unraid (User Scripts > Run Script)
#  1. extrait l'archive (optionnel)  2. construit l'image
#  3. ajoute les capacités Docker requises au template Unraid
#  Il reste ensuite UN clic : Docker > joal-smart-controller > Edit > Apply
#  Avec l'image publiée par GitHub (ghcr.io, voir README) ce script ne sert plus
#  aux mises à jour : Unraid propose « update » de lui-même.
# =====================================================================

# ---------- OPTIONS (à modifier ici) ----------
APP_DIR="/mnt/user/appdata/joal-smart-controller"
# Chemin du zip à extraire. Laisser vide si les fichiers sont déjà extraits.
ZIP_FILE=""
CONTAINER="joal-smart-controller"
IMAGE="joal-smart-controller:local"
TEMPLATE="/boot/config/plugins/dockerMan/templates-user/my-${CONTAINER}.xml"
BUILD_IMAGE="yes"        # yes / no
NO_CACHE="no"            # yes = reconstruction complète (plus lent)
PATCH_TEMPLATE="yes"     # yes / no
CLEAN_OLD_IMAGES="yes"   # yes = supprime les anciennes images de JOAL Smart Controller (jamais celles en service)
CLEAN_OLD_FILES="yes"    # yes = supprime du code livré (app/, clients/, tests/…) les fichiers absents du zip
CLEAN_BUILD_CACHE="yes"  # yes = supprime le cache de build Docker inutilisé depuis 7 jours
# ----------------------------------------------

set -uo pipefail
REQUIRED_CAPS="CHOWN FOWNER DAC_READ_SEARCH SETUID SETGID"
DEFAULT_PARAMS="--security-opt=no-new-privileges:true --cap-drop=ALL --cap-add=CHOWN --cap-add=FOWNER --cap-add=DAC_READ_SEARCH --cap-add=SETUID --cap-add=SETGID --init --stop-timeout 120"

step() { echo; echo "=== $* ==="; }
fail() { echo "ERREUR : $*"; exit 1; }

[ "$(id -u)" = "0" ] || fail "lancer en root (User Scripts le fait par défaut)"
[ -d "$APP_DIR" ] || fail "dossier introuvable : $APP_DIR"
cd "$APP_DIR" || fail "accès impossible à $APP_DIR"

# ---------- 1. Extraction ----------
if [ -n "$ZIP_FILE" ]; then
  step "Extraction de $ZIP_FILE"
  [ -f "$ZIP_FILE" ] || fail "zip introuvable : $ZIP_FILE"
  command -v unzip >/dev/null || fail "unzip absent : extraire le zip à la main puis laisser ZIP_FILE vide"
  # data/ et .env ne sont jamais écrasés.
  unzip -o -q "$ZIP_FILE" -x 'data/*' '.env' || fail "extraction échouée"
  if [ "$CLEAN_OLD_FILES" = "yes" ]; then
    # Fichiers d'anciennes versions (unzip -o écrase mais ne supprime jamais).
    # Uniquement dans le code livré : jamais data/, .env ni les autres fichiers du dossier.
    listing=$(mktemp) || fail "fichier temporaire impossible"
    unzip -Z1 "$ZIP_FILE" | sed 's#^\./##' | sort -u > "$listing" || fail "lecture du zip impossible"
    if grep -q '^app/main.py$' "$listing"; then
      removed=0
      while IFS= read -r file; do
        if ! grep -Fqx -- "$file" "$listing"; then
          rm -f -- "$file" && echo "  supprimé : $file" && removed=$((removed + 1))
        fi
      done < <(find app telemetry tests testbed clients ci .github -type f ! -path '*/__pycache__/*' 2>/dev/null | sort)
      find app telemetry tests testbed clients ci .github -mindepth 1 -type d -empty -delete 2>/dev/null
      echo "Fichiers obsolètes supprimés : $removed"
    else
      echo "Zip inattendu (app/main.py absent) : aucun fichier supprimé"
    fi
    rm -f "$listing"
  fi
fi

VERSION=$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' app/version.py 2>/dev/null)
echo "Version des fichiers : ${VERSION:-inconnue}"
[ -f docker-entrypoint.sh ] || fail "docker-entrypoint.sh absent : l'archive v1.23+ n'est pas extraite dans $APP_DIR"
grep -q '^!docker-entrypoint.sh$' .dockerignore 2>/dev/null || {
  echo "Correction de .dockerignore (entrypoint manquant)"
  sed -i 's#^!config.example.json$#!config.example.json\n!docker-entrypoint.sh#' .dockerignore
}

# ---------- 2. Build ----------
if [ "$BUILD_IMAGE" = "yes" ]; then
  step "Construction de l'image $IMAGE"
  opts=()
  [ "$NO_CACHE" = "yes" ] && opts+=(--no-cache)
  candidate="joal-smart-controller:verification-$$"
  trap 'docker image rm "$candidate" >/dev/null 2>&1 || true' EXIT
  tar --exclude='__pycache__' --exclude='*.pyc' -cf - Dockerfile .dockerignore app telemetry clients config.example.json docker-entrypoint.sh |
    docker build "${opts[@]}" -t "$candidate" - || fail "build Docker échoué (voir ci-dessus)"
  built=$(docker run --rm --user 99:100 --entrypoint python3 "$candidate" -B -c \
    'import main; print(main.APP_VERSION)') || fail "application illisible avec UID 99 / GID 100"
  echo "Version dans l'image : $built"
  [ -n "$VERSION" ] && [ "$built" = "$VERSION" ] || fail "version construite différente des fichiers"
  docker tag "$candidate" "$IMAGE" || fail "publication de l’image impossible"
fi

# ---------- 2b. Anciennes images ----------
if [ "$CLEAN_OLD_IMAGES" = "yes" ]; then
  step "Anciennes images de JOAL Smart Controller"
  # Seulement les images orphelines portant l'étiquette de l'application (v1.29.3+).
  # Docker ne supprime jamais une image utilisée par un conteneur, même arrêté.
  docker image prune -f --filter "label=app.joal-smart-controller=1" | tail -n 1
fi

if [ "$CLEAN_BUILD_CACHE" = "yes" ]; then
  step "Cache de build Docker"
  # Garde le cache récent (builds rapides), supprime celui inutilisé depuis 7 jours.
  docker builder prune -f --filter "until=168h" | tail -n 1
fi

# ---------- 3. Template Unraid ----------
if [ "$PATCH_TEMPLATE" = "yes" ]; then
  step "Capacités Docker dans le template"
  if [ ! -f "$TEMPLATE" ]; then
    echo "Template introuvable : $TEMPLATE"
    echo "Conteneur créé avec docker compose ? Alors : docker compose up -d --build"
  else
    current=$(sed -n 's:.*<ExtraParams>\(.*\)</ExtraParams>.*:\1:p' "$TEMPLATE" | head -n 1)
    if [ -z "$current" ]; then
      new="$DEFAULT_PARAMS"
    else
      new="$current"
      for cap in $REQUIRED_CAPS; do
        case " $new " in *" --cap-add=$cap "*) ;; *) new="$new --cap-add=$cap" ;; esac
      done
    fi
    if [ "$new" = "$current" ]; then
      echo "Déjà à jour : aucune modification"
    else
      backup="${TEMPLATE}.bak-$(date +%Y%m%d-%H%M%S)"
      cp "$TEMPLATE" "$backup" || fail "sauvegarde du template impossible"
      awk -v val="$new" '
        /<ExtraParams\/>/                  { sub(/<ExtraParams\/>/, "<ExtraParams>" val "</ExtraParams>"); done = 1 }
        /<ExtraParams>.*<\/ExtraParams>/   { sub(/<ExtraParams>.*<\/ExtraParams>/, "<ExtraParams>" val "</ExtraParams>"); done = 1 }
        /<\/Container>/ && !done           { print "  <ExtraParams>" val "</ExtraParams>"; done = 1 }
        { print }' "$backup" > "$TEMPLATE" || { cp "$backup" "$TEMPLATE"; fail "modification du template échouée (restauré)"; }
      echo "Template mis à jour (sauvegarde : $backup)"
      echo "Extra Parameters : $new"
    fi
  fi
fi

# ---------- Résumé ----------
step "Vérification des versions"
image_id=$(docker image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null)
image_version=$(docker run --rm --entrypoint python3 "$IMAGE" -c \
  'import sys; sys.path.insert(0, "/app"); from version import APP_VERSION; print(APP_VERSION)' 2>/dev/null)
echo "Fichiers extraits : ${VERSION:-inconnue}"
echo "Image construite  : ${image_version:-inconnue}"
if docker inspect "$CONTAINER" >/dev/null 2>&1; then
  running_id=$(docker inspect -f '{{.Image}}' "$CONTAINER")
  running_version=$(docker exec "$CONTAINER" python3 -c 'from version import APP_VERSION; print(APP_VERSION)' 2>/dev/null)
  echo "Conteneur actuel  : ${running_version:-arrêté} · capacités $(docker inspect -f '{{.HostConfig.CapAdd}}' "$CONTAINER")"
  if [ "$running_id" = "$image_id" ] && [ -n "$running_version" ] && [ "$running_version" = "$VERSION" ]; then
    echo "OK : le conteneur utilise déjà la nouvelle image."
  else
    echo
    echo ">>> La nouvelle version en cours d’exécution n’est pas confirmée."
    echo ">>> Docker > $CONTAINER > Edit > F5 (recharger) > Apply"
  fi
else
  echo "Conteneur $CONTAINER introuvable : le créer depuis le template Unraid."
fi
[ -n "${image_version:-}" ] && [ -n "${VERSION:-}" ] && [ "$image_version" != "$VERSION" ] && \
  echo ">>> ATTENTION : l'image ($image_version) ne correspond pas aux fichiers ($VERSION). Relancer avec NO_CACHE=\"yes\"."
echo "Après Apply : docker top $CONTAINER -eo pid,user,args"
