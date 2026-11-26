#!/bin/bash
# =====================================================================
#  JOAL Smart Controller - connexion d'Unraid au registre privé ghcr.io
#  User Scripts : planifier « At Startup of Array » (Unraid oublie la
#  connexion à chaque redémarrage), puis lancer une fois avec « Run Script ».
#  Le jeton GitHub n'a besoin que du droit « read:packages ».
# =====================================================================

# ---------- OPTIONS (à modifier ici) ----------
GHCR_USER="ton-compte-github"
GHCR_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"   # jeton « classic », droit read:packages uniquement
# ----------------------------------------------

set -uo pipefail
case "$GHCR_TOKEN" in
  ghp_x*|"") echo "ERREUR : renseigner GHCR_USER et GHCR_TOKEN en haut du script"; exit 1 ;;
esac
if printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin >/dev/null; then
  echo "Connecté à ghcr.io en tant que $GHCR_USER"
else
  echo "ERREUR : connexion à ghcr.io refusée (compte ou jeton incorrect, ou droit read:packages manquant)"
  exit 1
fi
