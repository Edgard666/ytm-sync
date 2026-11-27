# YTM Sync — v3.4.2

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
- Titres que j'aime, alerte de session YouTube expirée, graphique de couverture, journaux consultables.
- Home Assistant par MQTT (découverte automatique) ou API REST.
- Suggestions de correspondance avec score, aperçu des changements avant synchro, page Diagnostic.
- « J'aime » YouTube Music → favoris Navidrome ; export d'une playlist Navidrome vers YouTube Music.
- Surveillance automatique des nouvelles playlists, mixes YouTube (Supermix…), qualité audio faible à améliorer, doublons de la bibliothèque.
- Sauvegarde quotidienne (14 jours) et récupération automatique d'un fichier abîmé ; CSP stricte ; réponses compressées.
- Notifications Discord / Slack / webhook ; journal des corrections avec annulation ; appli installable, thème clair.
- Conteneur non-root (PUID/PGID, 99:100 par défaut), fichiers de configuration en 600, tests automatiques avant chaque image.
- Sécurité : CSRF, blocage après 5 échecs de connexion ; API pour Home Assistant (`/api/ext/status`, `/api/ext/sync`).

Toute la configuration se fait dans l'interface web (stockée dans `/config`).
Seules variables Docker : `TZ`, `PUID` / `PGID` (99 / 100) et `WEBUI_PORT` (8095).

## Installation (même schéma que JOAL Smart)

1. Créer le dépôt privé `Edgard666/ytm-sync` et y pousser ce dossier (branche `main`).
2. GitHub Actions construit `ghcr.io/edgard666/ytm-sync:latest` (+ tag de version).
3. Sur Unraid, copier `unraid/ytm-sync.xml` dans
   `/boot/config/plugins/dockerMan/templates-user/my-ytm-sync.xml`,
   puis Docker → Add Container → modèle « ytm-sync ».
   (Le serveur doit être connecté à ghcr.io comme pour JOAL : `docker login ghcr.io`.)
4. Ouvrir `http://IP-UNRAID:8095` et saisir le **code d'installation** affiché dans les journaux du conteneur
   (Unraid → Docker → icône ytm-sync → Logs), puis créer le mot de passe (10 caractères min.).

## Mises à jour automatiques des dépendances

- Les versions des dépendances sont **figées** (`requirements.txt`) et les actions GitHub épinglées sur un commit précis.
- **Dependabot** propose chaque lundi les mises à jour (Python, actions GitHub, image Docker) sous forme de *pull request* :
  les tests tournent automatiquement dessus. Si c'est vert, fusionne-la : l'image est reconstruite et « Update » apparaît dans Unraid.
- L'image est aussi **reconstruite chaque lundi** pour récupérer les correctifs de sécurité de l'image de base.

## Mise à jour

Modifier le code, **incrémenter `VERSION`**, pousser sur `main` → bouton « Update » dans Unraid.

## Fichiers dans /config

| Fichier | Contenu |
|---|---|
| `config.json` | Paramètres et playlists |
| `state.json` | État des titres, dernières synchros |
| `browser.json` | Session YouTube Music (sensible) |
| `logs/` | Journaux d'exécution |

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest -q tests
```
GitHub Actions lance ces tests avant chaque construction d'image : si un test échoue, l'image n'est pas publiée.

| Fichier | Ce qui est vérifié |
|---|---|
| `tests/test_matching.py` | Comparaison des titres (versions, durées, règles, qualité) |
| `tests/test_engine.py` | Moteur de synchro avec des faux clients en mémoire |
| `tests/test_web.py` | Interface : connexion, CSRF, blocage, sessions, sauvegardes |
| `tests/test_e2e.py` | **Bout en bout** : l'application parle en HTTP à de faux Navidrome, SoulSync et ntfy (`tests/fakes.py`) — synchro, comptes, suivi des téléchargements, échecs notifiés, ignorer/lier, protection playlist vide, favoris, export, mix, surveillance auto, qualité, doublons, sauvegarde, API externe, fuite de jeton |

`tests/test_audit.py` : un test par correctif de l'audit de la v3.4.2 (playlist perso jamais écrasée, heure invalide, secrets liés à leur adresse, restauration tout-ou-rien, session YouTube conservée, proxy, healthcheck, arrêt propre…).

Aucun service réel n'est contacté : les faux serveurs tournent dans le processus de test.

## Organisation du code

| Fichier | Rôle |
|---|---|
| `app/matching.py` | Comparaison des titres (normalisation, versions, similarité, qualité, règles) |
| `app/clients.py` | YouTube Music, Navidrome (+ bibliothèque en mémoire), SoulSync |
| `app/sync.py` | Synchronisation et planificateur |
| `app/monitor.py` | Suivi des téléchargements SoulSync |
| `app/export.py` | Export Navidrome → YouTube Music |
| `app/notify.py`, `app/mqtt.py` | Notifications, Home Assistant MQTT |
| `app/store.py` | Configuration, état, sauvegardes, récupération |
| `app/core.py` | Application web, sécurité (CSRF, CSP, sessions, proxy) |
| `app/services.py` | Logique partagée par les routes |
| `app/routes_*.py` | Pages et API, par thème |
| `app/main.py` | Point d'entrée |
