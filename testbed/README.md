# Banc d'essai

Environnement de démonstration et de test **sans aucun tracker réel** : un tracker HTTP local (`tracker.py`), le contrôleur et, en option, qBittorrent.

## Démarrage

```sh
cd /mnt/user/appdata/joal-smart-controller      # ou le dossier du projet
python3 testbed/make_torrents.py                 # 3 fichiers + .torrent dans testbed/generated/
docker compose -f compose.test.yaml up -d --build
```

- Contrôleur : `http://IP:8789` (mot de passe `banc-essai-123456`).
- État du tracker : `http://IP:6969/stats` (pairs, client de chaque pair, dernières annonces).

## Scénario de démonstration

1. Copier les profils JOAL (`*.client`) dans `testbed/controller-data/clients/`.
2. Créer une règle : domaine `tracker`, leechers minimum `0`, débits `0`, profil `qbittorrent-….client`. Désactiver `dryRun`.
3. Déposer les `.torrent` de `testbed/generated/torrents/` dans `testbed/controller-data/import/`.
4. Après une minute environ, `/stats` montre un pair `-qB…` avec `left: 0` : JOAL est vu comme seeder qBittorrent.
5. Retirer le profil `.client` de la règle : le tracker refuse l'annonce et l'interface affiche « Annonce refusée : Client refusé par le tracker ».

## Options du tracker

| Variable | Rôle |
|---|---|
| `ALLOWED_CLIENTS` | Préfixes `peer_id` acceptés (ex. `-qB,-TR`). Vide : tous. |
| `PASSKEY` | Exige `/<passkey>/announce` comme un tracker privé. |
| `TRACKER_INTERVAL` | Intervalle d'annonce renvoyé (secondes). |

## Test automatisé de bout en bout (sans Docker)

Avec Java 17+ et le `joal.jar` patché (étape `build` du Dockerfile) :

```sh
JOAL_JAR=/chemin/joal.jar JOAL_CLIENTS=/chemin/clients python3 tests/e2e_testbed.py
```

Le script vérifie l'import par dossier surveillé, l'annonce acceptée comme seeder qBittorrent, les compteurs, le graphique, la rotation du journal, la santé des threads, puis le refus d'un client non émulé.

`ALLOW_PRIVATE_TRACKERS=1` n'est destiné qu'au banc d'essai : en production, les trackers sur adresse privée restent refusés.
