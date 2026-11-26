# YTM Sync — v3.0.0

YouTube Music → Navidrome → SoulSync, avec interface web.

- Surveille plusieurs playlists YouTube Music (publiques ou privées).
- Compare chaque titre à la bibliothèque Navidrome (API Subsonic, comparaison tolérante).
- Crée / met à jour une playlist Navidrome miroir par playlist YouTube.
- Envoie les titres manquants dans la wishlist SoulSync et lance leur téléchargement.
- Planification, simulation, journal en direct, historique, suivi des titres.
- Suivi des téléchargements SoulSync par titre, scan Navidrome et resynchro automatiques.
- Notifications ntfy / Gotify / e-mail.
- Bibliothèque Navidrome en cache, règles d'exclusion, liens manuels, titres ignorés.
- Playlists par compte Navidrome, page Ménage (rien n'est jamais supprimé), sauvegarde / restauration.
- Sécurité : CSRF, blocage après 5 échecs de connexion ; API pour Home Assistant (`/api/ext/status`, `/api/ext/sync`).

Toute la configuration se fait dans l'interface web (stockée dans `/config`).
Seules variables Docker : `TZ` (et `WEBUI_PORT`, 8095 par défaut).

## Installation (même schéma que JOAL Smart)

1. Créer le dépôt privé `Edgard666/ytm-sync` et y pousser ce dossier (branche `main`).
2. GitHub Actions construit `ghcr.io/edgard666/ytm-sync:latest` (+ tag de version).
3. Sur Unraid, copier `unraid/ytm-sync.xml` dans
   `/boot/config/plugins/dockerMan/templates-user/my-ytm-sync.xml`,
   puis Docker → Add Container → modèle « ytm-sync ».
   (Le serveur doit être connecté à ghcr.io comme pour JOAL : `docker login ghcr.io`.)
4. Ouvrir `http://IP-UNRAID:8095`, créer le mot de passe, remplir Paramètres.

## Mise à jour

Modifier le code, **incrémenter `VERSION`**, pousser sur `main` → bouton « Update » dans Unraid.

## Fichiers dans /config

| Fichier | Contenu |
|---|---|
| `config.json` | Paramètres et playlists |
| `state.json` | État des titres, dernières synchros |
| `browser.json` | Session YouTube Music (sensible) |
| `logs/` | Journaux d'exécution |
