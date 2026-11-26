# Journal des versions

## v1.30.2 · Retrait groupé des torrents importés

- **Bouton « Retirer de JOAL (n) »** dans les barres de sélection (accueil, torrents, import) : retire en une fois les torrents importés cochés. Le compteur ne prend que les torrents de la bibliothèque JOAL ; les torrents qBittorrent de la sélection ne sont jamais touchés (la confirmation l'indique) et restent sélectionnés.
- API : `/api/solo/delete` accepte aussi `{"infoHashes": [...]}` (jusqu'à 500), traité en une seule lecture de la bibliothèque ; la réponse liste les torrents retirés et ceux déjà absents.
- **Correction** : retirer un torrent importé laissait sa règle affectée, son seuil de leechers individuel et son mode Stop/Forced dans la configuration. Ils sont maintenant effacés, sauf si le même torrent est dans qBittorrent (ils s'y appliquent encore) ou si qBittorrent ne répond pas.

## v1.30.1 · `.gitignore` protège les données

- `.gitignore` exclut désormais `data/` (passkeys dans les `.torrent`, réglages), `.env` (mot de passe) et les archives `.zip` : un `git add .` lancé par erreur dans le dossier d'installation n'envoie plus de secrets sur GitHub.

## v1.30.0 · Mises à jour automatiques de JOAL et des profils clients

- **Image construite et publiée par GitHub** (`.github/workflows/image.yml`) sur `ghcr.io` : à chaque envoi et chaque lundi, avec la dernière version de JOAL (Docker Hub), les dernières mises à jour de Java 21 et les derniers profils `.client` du dépôt JOAL. Unraid affiche alors « update ready ». Procédure dans le README.
- **Rien n'est publié sans tests** : tests unitaires, puis essai de bout en bout (`ci/smoke_image.py`) où JOAL annonce à un faux tracker avec un vrai profil et où la télémétrie doit remonter les chiffres, puis démarrage du contrôleur. Un JOAL qui casse le correctif de télémétrie bloque la publication. Une image aux entrées identiques n'est pas republiée (pas de fausse mise à jour).
- **Profils `.client` fournis avec l'image** (96, dont qBittorrent 5.3.0) et ajoutés au démarrage dans `/data/clients` s'ils y manquent. Un profil existant, même modifié, n'est jamais remplacé ni supprimé.
- **Version de JOAL** affichée en pied de page et dans `/api/health` (`joalVersion`).
- `Dockerfile` paramétrable (`JOAL_IMAGE`, `JDK_IMAGE`, `JRE_IMAGE`) : la construction locale garde les versions épinglées de la v1.29.4.
- `unraid/ghcr-login.sh` : connexion d'Unraid à ghcr.io pour une image privée.
- `update-joal.sh` : intègre les profils `clients/` à la construction locale et au nettoyage des fichiers obsolètes.

## v1.29.4 · Fiabilité : sessions bloquées, identité client, nettoyage

- **Session JOAL bloquée** : une session vivante sans aucune mesure 2 minutes après son lancement est arrêtée. Une session qui s'arrête d'elle-même (manque de mémoire, erreur) est détectée aussi. La raison, lue dans `joal.log` sous forme de libellé fixe (jamais d'URL ni de passkey), s'affiche sur les torrents et dans l'état de la règle (« JOAL arrêté : mémoire insuffisante · nouvelle tentative dans 12 min ») et crée une alerte ; une alerte de rétablissement suit au retour des mesures.
- **Relevé avec l'identité du client** : l'annonce de relevé du contrôleur (trackers qui refusent le scrape) reprend le profil `.client` des sessions JOAL (premier profil d'une règle active) : préfixe et format du peer_id, clé conservée par torrent, modèle de requête, encodage, port 49152 et en-têtes HTTP (User-Agent…), réponses gzip comprises. Les 90 profils fournis avec JOAL 2.1.37 sont reproduits. Sans profil exploitable, la requête générique d'avant est conservée.
- **Relevé unique** : il n'est utilisé que si la règle couvre tous les trackers du torrent (affectation manuelle, Forced, ou règle qui couvre tous ses trackers). Une règle limitée à certains trackers ne se fie qu'aux relevés de ses trackers, comme avant la v1.29.
- **`update-joal.sh`** : `CLEAN_OLD_FILES="yes"` supprime de `app/`, `telemetry/`, `tests/` et `testbed/` les fichiers absents du zip (jamais `data/`, `.env` ni les autres fichiers du dossier) ; `CLEAN_BUILD_CACHE="yes"` supprime le cache de build Docker inutilisé depuis 7 jours.
- **Images de base épinglées** par empreinte sha256 (JOAL 2.1.37, Temurin 21 JDK et JRE), celles de la v1.29.3 : une reconstruction donne toujours le même Java.

## v1.29.3 · Mémoire de JOAL adaptée aux gros torrents

- **Correction** : chaque session JOAL avait une mémoire fixe de 128 Mo. Avec un très gros `.torrent` (des centaines de milliers de morceaux, des milliers de fichiers), JOAL manquait de mémoire en le lisant (`OutOfMemoryError: Java heap space`). Le processus restait pourtant vivant sans rien seeder : session « active », upload « JOAL · Indisponible » et total global à 0.
- La mémoire de chaque session est maintenant calculée sur la taille de ses `.torrent` : 64 Mo + 12 Mo par Mio de métadonnées, au moins 128 Mo (ou `JOAL_XMX`), au plus 1536 Mo. Calibré sur JOAL 2.1.37 : 29 Mio de métadonnées demandent 256 Mo, 57 Mio en demandent 448.
- Un JOAL à court de mémoire s'arrête désormais (`-XX:+ExitOnOutOfMemoryError`) au lieu de rester bloqué : le contrôleur voit la session arrêtée.
- La mémoire choisie est écrite dans le journal du contrôleur à chaque lancement (« JOAL … : n torrent(s), mémoire -Xmx…m »).
- `update-joal.sh` : nouvelle option `CLEAN_OLD_IMAGES="yes"` qui supprime les anciennes images de JOAL Smart Controller après la construction (jamais une image utilisée par un conteneur). L'image porte une étiquette pour ne toucher qu'à elles.

## v1.29.2 · Secours par annonce pour tout scrape raté

- **Correction** : pour un torrent hors qBittorrent, l'annonce de relevé de secours ne partait que pour quelques refus précis (HTTP 403/404/405, pas d'URL de scrape…). Un délai dépassé, une erreur HTTP 5xx, un refus avec motif ou un scrape qui donne les seeders sans les leechers laissaient le torrent bloqué sur « Dernier relevé trop ancien ».
- Désormais, tout scrape raté ou incomplet d'un torrent hors qBittorrent passe par l'annonce de relevé (même conditions qu'avant : mode Auto, pas de dryRun ni de pause, pas de session JOAL en cours, torrent absent de qBittorrent). Si l'annonce est refusée elle aussi, les seeders donnés par le scrape sont gardés.
- Torrents de qBittorrent : inchangé.

## v1.29.1 · Relevés tenus à jour quand le tracker refuse le scrape

- **Correction** : un torrent hors qBittorrent dont le tracker refuse le scrape (fréquent sur les trackers privés : HTTP 403/404, pas d'URL de scrape) n'obtenait ses chiffres que par l'annonce d'arrêt faite juste après l'import ou au démarrage. Passé 45 minutes, il restait bloqué sur « Dernier relevé trop ancien » et ne s'activait plus.
- Cette annonce de relevé (événement *stopped*, sans transfert) est maintenant refaite à chaque passage de la surveillance des trackers (*intervalMinutes*, 15 min par défaut), uniquement si le scrape est refusé.
- Aucune annonce supplémentaire tant qu'une session JOAL tourne pour ce torrent : JOAL fournit déjà ses chiffres. Toujours rien en dryRun, en pause globale ou en mode Forced/Stop.

## v1.29.0 · Version allégée et relevés des torrents hors qBittorrent

- **Designs « Trame » et « Régie » retirés** : seuls « Classique » et « Baie » restent dans *Paramètres → Apparence*. Les fichiers HTML, CSS et polices de ces deux designs sont supprimés. Un choix « Trame » ou « Régie » enregistré, ou présent dans une sauvegarde v1.27/v1.28, repasse automatiquement en « Classique » (la sauvegarde reste restaurable).
- **Seeds et leeches des torrents hors qBittorrent toujours à jour** : la liste affiche le relevé valide le plus récent du torrent, quelle que soit son origine (scrape fait sous n'importe quelle règle, relevé à l'import, annonce JOAL ou lecture locale JOAL). Avant, un torrent en *Forced* ou affecté à « seed-sans-upload » restait à « inconnu », car le relevé était rangé sous une autre règle que celle affichée.
- **Un seul relevé suffit pour activer** un torrent hors qBittorrent : le seuil de leechers de la règle est comparé à ce relevé récent, même s'il vient d'une autre règle ou d'une annonce JOAL. Le seuil et la durée de validité des relevés (*maxAgeMinutes*) s'appliquent toujours.
- **Relevé immédiat** au passage en *Forced* et au démarrage aussi pour un torrent dont aucun tracker n'est couvert par une règle (relevé « import »), au lieu d'attendre la rotation normale.
- Torrents de qBittorrent : aucun changement (les confirmations du basculement restent inchangées).

## v1.28.0 · Nouveau design « Régie » : une vraie nouvelle mise en page

« Trame » ne changeait que l'habillage. « Régie » change aussi la structure de la page.

- **Navigation en barre du haut** (au lieu du menu latéral) : Accueil, Torrents, Règles, Surveillance, Système. Les pages d'une section (par exemple Paramètres, Sauvegardes, JSON avancé) sont des onglets sous le titre. Le voyant de connexion, le mode, les alertes, l'actualisation et la déconnexion restent dans la barre.
- **Accueil en deux zones** : grand compteur de débit simulé avec sa courbe sur une scène verte, et colonne des règles à côté (un liseré par règle : vert actif, orange en attente, gris désactivé). Les six indicateurs forment une bande, toujours visible (plus de section repliée).
- **Torrents en cartes** au lieu du tableau à 11 colonnes : plus de défilement horizontal. Le liseré de chaque carte prend la couleur du mode choisi (Auto bleu, Forced ambre, Stop gris). Les tris restent disponibles, présentés en pastilles « Trier par ». La barre d'actions groupées reste collée sous la barre du haut.
- **Paramètres en deux colonnes** (intitulé à gauche, réglages à droite) ; page de connexion à part (bande verte, carte à cheval dessus).
- **Thème** : Régie suit le système, avec *Clair* et *Sombre* au choix. Le thème choisi est conservé quand on passe de Trame à Régie.
- Police **Bricolage Grotesque** auto-hébergée (licence OFL, environ 130 Ko, un seul fichier variable), compatible avec la politique de sécurité du contenu.
- **Changement de mise en page** : passer à Régie (ou en revenir) recharge la page une fois et revient sur Paramètres. Entre Classique, Baie et Trame, le changement reste immédiat.
- **Aucune fonction modifiée** : `index-regie.html` reprend les mêmes identifiants et les mêmes éléments que `index.html` ; un test vérifie qu'aucun identifiant ne manque. Seuls ont changé le sélecteur de design (carte Régie, thème commun à Trame et Régie) et le serveur, qui choisit le gabarit de page selon le design (`template` dans `/api/interface-design`).
- « Classique », « Baie » et « Trame » sont conservés tels quels.

## v1.27.0 · Nouveau design « Trame »

- **Design « Trame »**, refait de zéro : barre latérale encre et zone de travail neutre, un seul accent outremer. Page de connexion assortie.
- **Bandeau d'accueil sombre** façon instrument : débit simulé en grand, courbe de l'heure écoulée et commandes réunis dans un même bloc.
- **Liseré d'état** à gauche de chaque ligne de tableau (vert : session JOAL active, orange : retenu ou en attente) et teinte légère des lignes actives.
- **Icônes de navigation** dessinées en CSS (une seule famille, à la place des glyphes Unicode, dont un sablier-émoji), sans image externe.
- **Thème** : « Trame » suit le thème du système par défaut ; *Paramètres → Apparence* propose aussi *Clair* et *Sombre* pour le forcer. Réglage enregistré côté serveur comme le design (`trame`, `trame-clair`, `trame-sombre` dans `data/interface.json`), valable pour tous les navigateurs et inclus dans la sauvegarde ZIP.
- Police **Archivo** auto-hébergée (licence OFL, environ 90 Ko, un seul fichier variable : largeur étendue pour les titres, chiffres tabulaires dans les tableaux), compatible avec la politique de sécurité du contenu (`'self'`).
- Les graphiques SVG gardent leurs couleurs dans les deux thèmes (couleurs remappées en CSS, sans modifier le JavaScript).
- « Classique » et « Baie » sont conservés tels quels. La carte « Trame » et le choix du thème sont ajoutés au sélecteur.
- Aucune fonction modifiée : même HTML (hors sélecteur) et mêmes identifiants pour les trois designs. Le serveur renvoie maintenant aussi le schéma de couleur du design choisi (`scheme`).

## v1.26.0 · Nouveau design « Baie » et choix du design

- **Design « Baie »**, refait de zéro : interface claire inspirée d'une baie de brassage. Façades en aluminium, titre de page sur étiquette de titreuse jaune, statuts affichés comme des voyants, barre d'envoi segmentée comme la carte des pièces d'un torrent, tableaux denses à rangées alternées, chiffres tabulaires. Page de connexion assortie.
- **Choix du design** dans *Paramètres → Apparence* : « Classique » (sombre, inchangé) ou « Baie ». Le changement est immédiat, sans rechargement. Il est enregistré côté serveur (`data/interface.json`, droits 0600), donc valable pour tous les navigateurs. La page est envoyée directement avec la bonne feuille de style, sans éclair de l'autre design.
- Polices Barlow et Barlow Condensed auto-hébergées (licence OFL, environ 110 Ko), compatibles avec la politique de sécurité du contenu (`'self'` uniquement).
- Les graphiques SVG gardent leurs couleurs dans le design clair : grille, textes et courbes sont remappés en CSS, sans modifier le JavaScript.
- Le choix du design est inclus dans la sauvegarde ZIP et validé à la restauration.
- Aucune fonction modifiée : même HTML et mêmes identifiants pour les deux designs.

## v1.25.8 · Explication de l'upload nul et finitions

- **Upload à 0 expliqué** : sur une session JOAL active sans débit, la ligne du torrent indique « Upload à 0 : aucun autre seeder signalé… » ou « … aucun leecher signalé… », d'après la dernière réponse du tracker vue par JOAL. Rien n'est affiché pour une règle volontairement à 0/0.
- **Sections mémorisées** : les groupes de la barre latérale et les sections repliables gardent leur état ouvert / fermé d'une visite à l'autre (stockage du navigateur, sans effet s'il est indisponible).
- **Icône** : favicon ajoutée. Supprime les réponses 401/404 à chaque chargement de page.
- **Tests navigateur** : les 13 scripts `browser_*.py` passent de nouveau (9 étaient obsolètes depuis plusieurs versions : sections repliables, import groupé, nouveaux libellés, ordre des lignes, attente asynchrone). Aucun ne révélait de bug de l'application.
- **Tests unitaires** : 6 fichiers ne fonctionnaient qu'en suite complète ; chacun se lance maintenant seul.
- Banc d'essai : le scénario 4 vérifie aussi l'apparition puis la disparition de l'explication.

## v1.25.7 · Vérification approfondie

- Correctif (présent depuis la v1.22.9) : les sessions Forced (`:forced`, `solo-:forced`) n'étaient jamais enregistrées dans le diagnostic (« Journal diagnostic indisponible (start_rule) »). Elles apparaissent maintenant dans l'historique.
- Interface : « Mode Forced » / « Mode Forced (sans upload) » au lieu des identifiants internes.
- Banc d'essai : scénario 4, torrent importé en Forced avec leecher et seeder tiers. Il échoue sur la v1.25.5 (upload 0) et passe sur la v1.25.6.
- Rappel JOAL : aucun upload n'est simulé tant que le tracker ne signale aucun autre seeder, même avec beaucoup de leechers.

## v1.25.6 · Forced redonne un vrai débit

- Correctif : en mode Forced, un torrent rattaché à la règle « sans upload » uniquement par ses trackers (cas des torrents importés) reçoit de nouveau les débits du premier profil avec client. La v1.25.4 lui gardait un débit nul, d'où un upload simulé à 0 malgré les leechers.
- Un torrent explicitement affecté à une règle 0/0 reste à 0 en Forced (inchangé).
- Un ancien `simulation-modes.json` contenant `forced-zero` est relu comme un Forced normal.

## v1.25.5 · Démarrage sur Unraid

Les droits de lecture du code et de la WebUI sont fixés dans l’image. Sa construction vérifie les imports Python et les deux pages avec l’UID 99 et le GID 100. Le script de mise à jour vérifie la version du candidat avant de le publier.

## v1.25.4 · Corrections ciblées

- Pause totale : préparation du journal avant le lancement de JOAL et arrêt du processus si sa supervision échoue.
- Diagnostic : dernier résultat et heure par domaine identifié dans la session, avec résumé cliquable. Une réponse sans domaine identifiable reste libellée « Moteur JOAL ».
- Forced : une règle automatique effective à 0/0 conserve son débit nul au passage en Forced et après redémarrage. Auto ou Stop annule ce choix.
- Dossier surveillé : deux observations stables avant import, puis une nouvelle tentative avant de déplacer un fichier en erreur.

## v1.25.3 · Allègement et efficacité (sans changement de fonction)

- **Moins d'écritures disque** : `diagnostics.json` n'est plus réécrit (avec fsync) à chaque cycle d'évaluation, mais uniquement lors d'un vrai changement d'état. En régime stable, 1 écriture au lieu d'une par cycle.
- **Mémoire des JVM JOAL plafonnée** : chaque processus JOAL démarre avec `-Xmx128m -XX:+UseSerialGC` (réglable par `JOAL_XMX` ou `JOAL_JAVA_OPTS`). Évite qu'une JVM occupe par défaut jusqu'à 25 % de la RAM de l'hôte. Mesuré : ~224 → ~200 Mo par processus, avec un plafond garanti.
- Aucune fonction ni aucun comportement visible n'est modifié.

Note : les relectures répétées de l'état qBittorrent dans un cycle d'évaluation ont été conservées volontairement — elles garantissent qu'une session JOAL ne démarre jamais sur un torrent qui vient de reprendre dans qBittorrent.

## v1.25.2 · Relevé immédiat au démarrage

- Au lancement du contrôleur, tous les torrents dont le dernier relevé est trop ancien (au-delà de « validité des relevés ») sont re-scrapés immédiatement, en priorité. Plus besoin de changer de mode après un redémarrage.
- Les torrents en Stop et ceux qui ont déjà un relevé utilisable sont ignorés, pour ménager les trackers.
- Le journal du conteneur indique le nombre de torrents concernés.

## v1.25.1 · Relevé immédiat au passage en Auto / Forced

- Passer un torrent en **Auto** ou **Forced** (ligne ou sélection groupée) déclenche tout de suite un relevé seeders / leechers de ses trackers, prioritaire sur la rotation normale. Fini le « Dernier relevé trop ancien » après un Stop prolongé.
- Dès que ce relevé arrive, l'évaluation est relancée (torrents qBittorrent et JOAL uniquement) : la session démarre en quelques secondes si le seuil est atteint.
- Pendant l'attente, la ligne affiche « Relevé des trackers en cours… ».
- Garde-fou : au plus un relevé forcé par torrent et par minute, pour ne pas surcharger les trackers privés en cas de clics répétés.
- Banc d'essai : nouveau scénario « Stop → Auto » dans `tests/e2e_testbed.py`.

## v1.25.0 · Refonte visuelle (sans changement de fonction)

- Barre latérale épurée : groupes repliables sans cadres, indicateur de page active, déconnexion en bas à côté de l'état de connexion.
- Interrupteurs à bascule pour dryRun, relevés, basculement, nettoyage et « Activée » des règles.
- Barre d'actions groupées (Stop / Auto / Forced…) mise en forme, avec couleur par action.
- Tableaux : lignes alignées au centre, en-têtes fixes, puce de règle sur une seule ligne, survol plus lisible.
- Cartes d'indicateurs plus compactes, ombres et rayons homogènes, focus clavier visible.
- Règles : aide affichée une seule fois en haut, règle désactivée atténuée.
- Champs de fichier, barres de défilement et boîtes de dialogue harmonisés avec le thème sombre.
- Tous les identifiants HTML et comportements JavaScript sont inchangés.

## v1.24.0 · Dossier surveillé, santé, plages horaires, graphiques et banc d'essai

- **Rotation des journaux** : `runtime/*/joal.log` est limité à 5 Mio × 3 archives (`LOG_MAX_MB`, `LOG_BACKUPS`). Les anciens journaux trop gros sont archivés au démarrage.
- **Dossier surveillé** : les `.torrent` déposés dans `data/import/` sont importés toutes les 15 s, puis rangés dans `traités/`, `doublons/` ou `erreurs/` (avec un `.erreur.txt`). Un fichier en cours de copie est ignoré tant qu'il change.
- **Motif de refus des annonces** : le greffon Java écoute les événements d'annonce de JOAL. La ligne du torrent affiche « Annonce refusée : … » (client refusé, torrent inconnu, authentification, HTTP 4xx…). Les URL et passkeys sont masquées dès la sortie de JOAL.
- **Page Santé** : état de chaque thread (dernière exécution, redémarrages, dernière erreur), version, espace disque, taille des journaux, dossier surveillé.
- **Plages horaires par règle** : jours et heures (y compris de nuit, ex. 22:00 → 07:00). Hors plage, les sessions s'arrêtent et la raison s'affiche. Le mode Forced les ignore.
- **Graphique seeders / leechers par torrent** (bouton « Graphique S/L ») : relevés des trackers et réponses d'annonce JOAL, sans inventer de zéros.
- **Banc d'essai** : tracker HTTP de test (`testbed/tracker.py`, peut imiter un tracker privé), générateur de torrents, `compose.test.yaml` et test de bout en bout avec le vrai JOAL (`tests/e2e_testbed.py`).
- Correctif : à 0 kB/s, le greffon publie désormais les compteurs seeders / leechers (JOAL ne listait pas ces torrents dans sa table de débits).
- Correctif : suppression des styles en ligne bloqués par la CSP de l'interface (erreurs de console).
- `ALLOW_PRIVATE_TRACKERS=1` (banc d'essai uniquement) autorise les trackers sur adresse privée.
- Image : `tzdata` installé pour les plages horaires (variable `TZ`).

## v1.23.2 · Contrôle de version

- `update-joal.sh` compare la version des fichiers, de l'image construite et du conteneur en cours, et signale clairement quand **Apply** est encore nécessaire.
- La version apparaît dans les logs de démarrage et dans `/healthz`.

## v1.23.1 · Seed sans upload via le moteur JOAL

- Les règles à débit 0 (seed sans upload) passent désormais par **JOAL à 0 kB/s** avec un profil `.client` : celui de la règle, sinon celui de la première règle qui en a un. Le tracker voit un client émulé (peer_id, key, User-Agent) au lieu d'un `peer_id` aléatoire avec `User-Agent: Python-urllib`, que les trackers privés refusent.
- Les compteurs seeders / leechers des torrents importés sont fournis par les réponses d'annonce de JOAL.
- Le script de présence n'est plus qu'un secours quand aucun profil `.client` n'existe (avertissement dans les logs). Son journal indique maintenant le motif de refus.

## v1.23.0 · Robustesse, sécurité et mise à jour à chaud

**Corrections**
- La connexion qBittorrent modifiée dans la WebUI s'applique **immédiatement**, sans redémarrer le conteneur. Les threads qBittorrent démarrent aussi si l'URL est ajoutée après le démarrage.
- Threads supervisés : une exception imprévue ne désactive plus définitivement une surveillance. Les threads de garde et JOAL uniquement arrêtent leurs sessions avant de redémarrer.
- Garde rapide : correction d'un accès non verrouillé à la liste des processus JOAL qui pouvait tuer le thread de sécurité.
- Nettoyage : plus aucun appel réseau pendant que le verrou des processus JOAL est tenu. Le torrent est réservé (`PENDING_DELETE`) avant les vérifications.
- Démarrage et arrêt qBittorrent : confirmation avec plusieurs relectures (qBittorrent applique ces changements en différé). Fini les faux « Reprise non confirmée ».
- Compatibilité qBittorrent 4.x : repli automatique sur `pause` et `resume` si `stop` et `start` renvoient 404.
- `validate()` : une section `swarm`, `cleanup` ou `handoff` mal typée renvoie une erreur 400 lisible au lieu de couper la connexion.
- Une règle active avec upload mais sans profil `.client` est refusée **à l'enregistrement** (un `policy.json` existant continue de se charger).
- Une restauration ZIP invalide en attente est mise de côté (`.restore-rejected-*.zip`) au lieu de faire planter le conteneur en boucle. Un `simulation-modes.json` invalide est aussi mis de côté (retour en Auto).
- `qbit-connection.json` corrompu : les valeurs Docker sont utilisées, le démarrage n'échoue plus.
- Télémétrie JOAL mal formée ignorée ; le thread d'historique des débits ne peut plus mourir.
- Arrêt du conteneur : le gestionnaire de signal pose seulement un drapeau. L'arrêt (avec réseau) s'exécute une seule fois dans `main()`.
- Les sessions de présence (débit 0) qui plantent sont désormais journalisées.

**Sécurité**
- Le conteneur tourne en **non-root** (`PUID=99`, `PGID=100` par défaut). `/data` est attribué une fois au démarrage, puis les droits root sont abandonnés.
- Permissions par défaut des `.torrent` : `0660` et `0770` (au lieu de `0666` et `0777`) car ils contiennent des passkeys.
- `TRUSTED_PROXIES` : derrière un reverse proxy, la limite de tentatives de connexion porte sur la vraie IP (`X-Forwarded-For`), uniquement si elle vient du proxy déclaré.
- `/healthz` (public, sans données) et `HEALTHCHECK` Docker.

**Mise à jour depuis 1.22.x** : extraire en conservant `data/` et `.env`, puis lancer `unraid/update-joal.sh` (User Scripts). Il construit l'image et ajoute les capacités au template Unraid ; il reste à cliquer **Docker → Edit → Apply**. Le premier démarrage attribue `data/` à `99:100`. Sans les nouvelles capacités, le conteneur démarre quand même, en root, avec un avertissement dans les logs.

# v1.19.0 · Commandes individuelles et groupées

Sur Accueil, Torrents suivis et Bibliothèque JOAL uniquement, choisir le mode sur une ligne ou cocher plusieurs torrents puis utiliser Stop, Auto ou Forced. Le choix est mémorisé dans data/simulation-modes.json. Auto est la valeur initiale. Stop exclut le torrent. Forced utilise les trackers du torrent et le client et les débits du premier profil actif disposant d’un client (à défaut, du premier profil avec client). La sélection Forced ignore le seuil de leechers et le ratio cible. La pause globale, le dryRun, le plafond global, les contrôles de concurrence et la priorité qBittorrent continuent de s’appliquer. Pour un torrent déjà actif dans qBittorrent, l’arrêter avant de lancer la simulation.

# v1.18.0 · Accueil, pause globale et pairs JOAL

Le bouton de l’accueil suspend les simulations sur toutes les règles, y compris JOAL uniquement. La pause persiste après redémarrage. Reprendre réactive l’évaluation habituelle et respecte dryRun et les délais anti-relance.

Les tableaux utilisent les derniers compteurs disponibles dans les réponses d’annonce reçues par JOAL, avec repli par compteur sur les relevés existants. Un point d’interrogation indique une donnée indisponible.

# v1.17.4 · Compteurs de session JOAL

Le moteur publie chaque seconde le débit et le compteur interne de chaque torrent. Les vues se rafraîchissent toutes les cinq secondes. Le volume repart de zéro au démarrage d’une nouvelle session.

Extraire les fichiers en conservant data/ et .env, puis reconstruire l’image locale.

```sh
cd /mnt/user/appdata/joal-smart-controller
docker build -t joal-smart-controller:local .
```

Unraid → Docker → joal-smart-controller → Edit → Apply.

## v1.17.3 · Débits simulés JOAL

Le total de toutes les instances JOAL gérées figure dans la Vue d’ensemble et dans Torrents. Le débit simulé individuel apparaît sous le nom de chaque torrent dans ces deux vues. L’interface est actualisée toutes les 5 secondes. Le total couvre aussi les torrents « JOAL uniquement », indépendamment des filtres et de la pagination. « Indisponible » indique qu’une mesure manque, notamment au démarrage ou après une perte de connexion au moteur.

La collecte utilise le flux de vitesses natif de JOAL 2.1.37 sur la boucle locale du conteneur. Reconstruire l’image avec `docker compose up -d --build` en conservant le dossier `data/` et le fichier `.env`.

# v1.17.2 — Indicateurs de seeders/leechers et activité JOAL

- Les scrapes HTTP(S) et UDP lisent désormais `complete` (seeders) en plus de `incomplete` (leechers), dans **la même requête**.
- Les nombres sont indicatifs et associés aux trackers ayant répondu ; une couverture partielle n’est jamais présentée comme l’ensemble du swarm. Aucun nombre n’est fabriqué pour une réponse absente.
- La bibliothèque JOAL uniquement, les listes unifiées et la page de relevés affichent `S / L` (seeders / leechers), avec `?` lorsque la mesure est inconnue.
- Les données qBittorrent peuvent être affichées comme secours, explicitement signalées comme non datées, sans servir aux décisions automatiques.
- Pendant une session JOAL uniquement active, la colonne d’activité présente une barre **indéterminée**, pas un pourcentage fictif de volume simulé. Les octets annoncés par JOAL ne sont toujours pas mesurés : le moteur fourni ne les expose pas à cette intégration.
- La sélection, la suppression et l'anti-basculement ne changent pas.

# Correctifs 1.17.1

- `minLeechers: 0` désactive le **critère de nombre de leechers** : le mode JOAL uniquement peut rester éligible sans scrape valide. Cela ne crée pas une fausse valeur de 0 et ne contourne pas les autres règles (tracker, doublon qBittorrent, quota, dryRun).
- Dans **Importer → Bibliothèque JOAL uniquement**, tous les domaines des torrents peuvent être cliqués pour les ajouter à une règle existante (également si déjà associés à une autre règle).
- La liste commune, l’infobulle et l’éditeur des règles affichent ce nouveau sens du seuil zéro.

# Version 1.17.1 — Seuil zéro sans scrape et trackers cliquables

- Dans **Vue d’ensemble**, **Torrents suivis** et **Importer → Bibliothèque JOAL uniquement**, la colonne **Règle** est maintenant cliquable (✎).
- Par défaut **Automatique** : règle choisie à partir des domaines des trackers et du seuil effectif, selon les priorités de la v1.16.2.
- **Manuelle** : choisir explicitement toute règle **activée** pour un infohash. Le choix est persisté sous `torrentRuleOverrides` dans `data/policy.json`. Il affecte uniquement ce torrent ; les domaines et le comportement des autres torrents restent inchangés. Si la règle choisie ne couvre pas naturellement ses trackers, cette affectation explicite autorise seulement les URL déjà présentes dans le `.torrent` pour ce torrent, sans ajouter ces domaines à la règle globale. Aucun nouveau torrent n'est automatiquement concerné.
- Si la règle manuelle est ensuite désactivée ou supprimée, le torrent est mis en attente ; il **ne retombe pas silencieusement** sur une autre règle. Choisir **Automatique** pour restaurer le fonctionnement initial.
- Le seuil `minLeechers=0` est maintenant autorisé, y compris dans les exceptions par infohash. **Zéro mesuré est différent d'inconnu** : une réponse `scrape` valide indiquant 0 est exploitable ; une réponse absente, périmée ou refusée reste bloquante. Les confirmations et les limites de sessions sont conservées.
- Les fichiers `.torrent` existants, les tags et les données qBittorrent ne sont pas modifiés par le choix de règle. Les sauvegardes de configuration incluent les affectations manuelles.
- Pour appliquer : conserver `data/` et `.env`, extraire le code, reconstruire `docker build --no-cache -t joal-smart-controller:local .`, puis recréer dans **Docker → Edit → Apply**.

**Limites** : le suivi des leechers dépend toujours des réponses des trackers. Aucun chiffre absent n'est inventé et aucune annonce fictive n'est considérée comme un transfert réel.

---

# Version 1.16.2 — Trackers partagés entre règles

- Un même domaine de tracker peut maintenant appartenir à plusieurs règles activées. Les sous-domaines peuvent également se chevaucher.
- En cliquant sur **n'importe quel domaine** dans la liste des torrents (y compris un domaine déjà couvert), tu peux choisir une nouvelle règle de destination. Un domaine déjà présent dans **cette** règle ne sera pas ajouté deux fois.
- Lorsque plusieurs règles peuvent s'appliquer à un torrent, une seule est retenue. Priorité au **seuil de leechers effectif le plus bas** (tenant compte des exceptions par infohash), puis à l'absence de plafond de ratio ou au ratio cible le plus haut, puis à la plus grande capacité de sessions. Les limites globales et par règle restent obligatoires. Une règle alternative peut être utilisée si la préférée est à pleine capacité.
- L'affichage des torrents surveillés et la bibliothèque « JOAL uniquement » utilisent la même politique de priorité.
- Les réglages et les fichiers présents dans `data/` et `.env` restent inchangés.
- La version du serveur et des deux pages Web est maintenant `1.16.2`.

# Version 1.16.1 — Conservation des noms originaux des fichiers .torrent

- Les nouveaux fichiers importés dans la WebUI sont sauvegardés dans `/data/torrents/` **avec leur nom de fichier d’origine**, y compris les espaces et caractères Unicode. L’infohash reste la clé d’identification du torrent et de détection des doublons.
- Si deux torrents distincts ont exactement le même nom de fichier, le second reçoit un suffixe basé sur son infohash pour éviter tout écrasement ; le nom stocké est retourné par l’API.
- Les anciens fichiers nommés `<infohash>.torrent` restent compatibles, visibles, supprimables et sauvegardables. **Ils ne sont pas renommés** : leur nom d’origine ne peut pas être reconstitué à partir des seules métadonnées avec certitude.
- Les noms dangereux (chemin, caractères de contrôle, nom trop long) sont refusés plutôt que réécrits silencieusement.
- Les sauvegardes/restaurations ZIP reconnaissent les noms d’origine et conservent la détection de doublons par infohash.
- Les permissions Unraid configurées dans la v1.16 ne changent pas.

Après extraction sur l’installation existante en conservant `data/` et `.env`, reconstruire puis recréer le conteneur via **Unraid → Docker → Edit → Apply** :

```sh
cd /mnt/user/appdata/joal-smart-controller
docker build -t joal-smart-controller:local .
```

---

# Mise à jour v1.16 — importation multiple et accès aux fichiers (Unraid)

- Sélection de 1 à 30 fichiers `.torrent` en une fois, analyse automatique et import sans fenêtre de confirmation.
- Retour détaillé par fichier : importé, doublon, erreur. Un mauvais fichier n'empêche pas les autres de s'importer.
- Stockage dans `/data/torrents`, correspondant à `/mnt/user/appdata/joal-smart-controller/data/torrents` sur Unraid.
- Par défaut, fichiers en `0666` (rw-rw-rw-) et dossier en `0777` (rwxrwxrwx), tentative d'attribution `nobody:users` (UID 99, GID 100) ; les anciens fichiers `.torrent` sont aussi ajustés au démarrage.
- Dans Docker, seules les capacités CHOWN et FOWNER sont rajoutées pour cette gestion. Certains types de volumes peuvent malgré cela refuser chmod/chown.

**Avertissement de sécurité :** les fichiers `.torrent` peuvent contenir des URL de tracker avec passkey. Les rendre lisibles et modifiables par tous les utilisateurs locaux n'est pas recommandé. Pour des permissions plus restrictives, régler `TORRENT_FILE_MODE=0660` et `TORRENT_DIR_MODE=0770` dans le template Unraid ou `.env`.

Si le conteneur ne peut toujours pas écrire après cette mise à jour (volume Unraid existant avec ACL restrictives), effectuer UNE FOIS sur le terminal de l'hôte Unraid :

```bash
mkdir -p /mnt/user/appdata/joal-smart-controller/data/torrents
chown -R nobody:users /mnt/user/appdata/joal-smart-controller/data/torrents
find /mnt/user/appdata/joal-smart-controller/data/torrents -type d -exec chmod 777 {} +
find /mnt/user/appdata/joal-smart-controller/data/torrents -type f -name '*.torrent' -exec chmod 666 {} +
```

Ne pas appliquer ces droits à `policy.json`, aux fichiers de comptes, aux autres dossiers `data/` ni à la racine de l'Appdata.

## Mise à jour Docker

Sauvegarder `data/` et `.env` ; remplacer uniquement les fichiers de code ; reconstruire l'image ; faire **Edit → Apply** dans Unraid pour recréer le conteneur.

```bash
cd /mnt/user/appdata/joal-smart-controller
docker build --no-cache -t joal-smart-controller:local .
```

## Correctif d'affichage v1.15 (archive corrigée)

Le numéro de version de la page de connexion et de l'interface principale est désormais fourni par **une unique constante** (`app/version.py`) au moment où le serveur livre les pages. La page de connexion affichait auparavant v1.13 malgré un moteur v1.15. Le moteur et les deux pages indiquent désormais la version courante, v1.16.

**Important :** si la WebUI et la page de connexion indiquent encore v1.13 après installation, une ancienne image Docker est probablement utilisée. Vérifier sur Unraid :

```bash
docker exec joal-smart-controller python3 -c 'from version import APP_VERSION; print(APP_VERSION)'
docker inspect -f 'Image du conteneur : {{.Image}}' joal-smart-controller
docker image inspect -f 'Image locale construite : {{.Id}}' joal-smart-controller:local
```

Les deux identifiants d'image doivent correspondre. En cas de différence, refaire **Edit → Apply** dans Unraid après la reconstruction. Conserver le dossier `data/` et le fichier `.env` à chaque mise à jour.

---

# JOAL Smart Controller v1.15 — Diagnostic des scrapes et liste unifiée

Cette version conserve les paramètres et la bibliothèque de la v1.14. **Aucune migration de `data/` n'est nécessaire**.

## Vue d'ensemble et Torrents suivis

- **Vue d’ensemble** : la liste regroupe désormais **tous** les torrents tagués dans qBittorrent et les fichiers `.torrent` importés en mode **JOAL uniquement**. Une colonne *Mode* et une recherche permettent de les distinguer. La table peut défiler verticalement dans la même page.
- **Torrents suivis** : même liste unifiée, avec tri, pagination (10/20/50/100/500), colonnes redimensionnables, barre horizontale et nouveau filtre par mode. Les commandes « Vers JOAL / Vers qBittorrent » restent réservées aux torrents gérés par qBittorrent ; le mode JOAL uniquement ne déclenche jamais un basculement qBittorrent.
- Le menu **Importer** continue de montrer la bibliothèque JOAL uniquement pour la gestion et la suppression des fichiers. Les fichiers restent exclusivement dans `/mnt/user/appdata/joal-smart-controller/data/torrents/`.
- L'absence de connexion à qBittorrent n'empêche pas l'affichage de la bibliothèque JOAL uniquement.

## Diagnostic de chaque tracker

- Dans **Torrents suivis**, **Vue d'ensemble** et **Relevés autonomes**, cliquer **Diagnostic trackers**. La fenêtre affiche chaque domaine interrogé, le résultat et la dernière tentative.
- Motifs distingués : absence d'URL `scrape` standard, réponse HTTP 401/403/404/429, panne DNS, timeout, réponse UDP invalide, protocole non pris en charge, données bencode incohérentes, etc. Une erreur inconnue est remplacée par une indication générique.
- **Aucune URL de tracker ni passkey n'est exposée dans l'API de consultation ou la WebUI** : seuls les domaines et des motifs prédéfinis sont affichés. Les URL privées restent dans le registre JSON local de `/data`.
- Un `scrape` réussi pour un seul tracker peut être affiché comme **partiel**, ce qui ne signifie pas qu'il représente l'ensemble du swarm. Un refus de `scrape` ne signifie pas 0 leecher, et des valeurs qBittorrent sans horodatage ne sont pas considérées comme des relevés frais.
- Les nouveaux motifs apparaissent **après la prochaine interrogation `scrape`** (15 minutes minimum par défaut, répartie entre les torrents surveillés). Les anciens états ne permettent pas de reconstruire la cause d'une erreur passée.

## Correction permissions Unraid (`policy.json`)

Cette version corrige aussi l'arrêt au démarrage : `PermissionError: [Errno 1] Operation not permitted: '/data/policy.json'`. Sur certains volumes /mnt/user d'Unraid, ou après une modification des propriétaires/ACL, un fichier peut rester lisible et modifiable même si `chmod(0600)` est refusé. Le contrôleur **ne s'arrête plus pour ce seul refus** : il enregistre un avertissement. Si le fichier est réellement inaccessible à l'utilisateur du conteneur, il s'arrête avec une explication, sans assouplir ses permissions automatiquement.

Pour diagnostiquer manuellement :

```bash
docker inspect joal-smart-controller --format 'Utilisateur : {{.Config.User}}'
ls -ln /mnt/user/appdata/joal-smart-controller/data/policy.json
```

Si le conteneur utilise bien root (Dockerfile/template fourni, sauf personnalisation) et que les permissions du fichier sont incorrectes :

```bash
chown 0:0 /mnt/user/appdata/joal-smart-controller/data/policy.json
chmod 600 /mnt/user/appdata/joal-smart-controller/data/policy.json
```

Ne pas exécuter de `chmod -R 777` sur `data/` : ses fichiers `.torrent` et son registre peuvent contenir des passkeys privées.

## Mise à jour Unraid

1. Sauvegarder `data/` et `.env` si présent, puis extraire ce ZIP par-dessus le code existant **sans supprimer le dossier `data/`**.
2. Depuis le terminal Unraid :

```bash
cd /mnt/user/appdata/joal-smart-controller
docker build -t joal-smart-controller:local .
```

3. Docker → joal-smart-controller → Edit → Apply, puis recharger la WebUI avec Ctrl+F5.
4. Vérifier les deux modes dans la vue d'ensemble, puis ouvrir un « Diagnostic trackers » pour un torrent dont le scrape est indisponible.

Tests locaux : `PYTHONPATH=app:tests python3 -m unittest discover -s tests -p 'test_*.py' -q` ; `node --check app/static/app.js` ; `node --test tests/test_table_sort.cjs` ; `python3 tests/browser_v115_unified.py` (avec Chromium/Playwright).

**Limites :** aucun diagnostic ne prouve qu'un tracker accepte des statistiques fictives ; l'application ne mesure pas les volumes internes annoncés par JOAL. Le test sur une instance réelle qBittorrent et sur des trackers privés reste à faire.

---

# JOAL Smart Controller v1.14 — Bibliothèque « JOAL uniquement » dans l’appdata Unraid

## Changement de comportement des importations WebUI

**Tout nouveau fichier `.torrent` importé par la page Web est conservé dans**
`/data/torrents/<infohash-v1>.torrent` **et n’est pas envoyé à qBittorrent**.
Sur Unraid avec le modèle XML fourni, ce dossier correspond à :

`/mnt/user/appdata/joal-smart-controller/data/torrents/`

- Il s'agit des **métadonnées .torrent** (incluant éventuellement les passkeys des trackers), pas des données téléchargées. Les fichiers sont privés (`0600`) et le dossier est privé (`0700`).
- L'import est disponible même sans règle active : le torrent est enregistré puis affiché « Aucune règle active ». Si un domaine correspond à une règle, sa surveillance commence automatiquement.
- Chaque torrent conserve son identité par infohash et est visible dans la section **« Bibliothèque JOAL uniquement »** du menu **Importer** (nom, domaines, règle, leechers, seuil, état). L’option « Retirer » enlève uniquement ce `.torrent` de la bibliothèque, jamais les données qBittorrent.
- Ces imports ne nécessitent **pas de données téléchargées**. Le contrôleur réutilise les relevés indépendants `scrape` HTTP(S)/UDP ; aucune valeur ancienne ou inconnue n'est assimilée à zéro.
- Si le nombre de leechers est inférieur au minimum de la règle, une session JOAL autonome est suspendue. Elle redevient admissible quand le seuil est confirmé. Une valeur élevée (minimum + 5 par défaut) nécessite un seul relevé frais ; sinon deux relevés distincts sont requis.
- Les torrents importés par cette voie **ne basculent jamais dans qBittorrent**. L'ancien fonctionnement **Tracker + tag** reste disponible pour les torrents qui sont déjà dans qBittorrent.
- La limite globale `maxCandidates` est partagée ; en cas de saturation, la priorité est donnée aux sessions qBittorrent. Les règles et le mode `dryRun` restent applicables.
- La bibliothèque autonome et le registre de ses trackers continuent de fonctionner si l'API qBittorrent est indisponible. Il est également possible de ne pas renseigner `QBIT_URL` si seuls les imports JOAL sont utilisés.
- La sauvegarde complète de la WebUI inclut maintenant les `torrents/*.torrent` (avec leurs passkeys éventuelles). Le rétablissement d'une sauvegarde force `dryRun: true`.

**Conflits** : lorsque qBittorrent est disponible, le contrôleur lit périodiquement son inventaire (environ une fois par minute) et bloque le mode JOAL uniquement si le même infohash y figure. Aucune suppression ni commande qBittorrent n’est effectuée. Si l’API qBittorrent est inaccessible, les nouveaux doublons ajoutés pendant l’interruption ne peuvent pas être détectés : éviter de charger le même torrent dans les deux clients.

**Migration** : les imports réalisés dans les versions 1.13 et antérieures restent dans qBittorrent. Le système ne les migre pas automatiquement : il serait impossible de les distinguer sûrement de torrents tagués manuellement. Les **nouveaux** imports WebUI utilisent exclusivement `/data/torrents/`.

**Limite technique** : les torrents BitTorrent v2 uniquement ne sont pas pris en charge par ce mode. En l’absence de `scrape` utilisable sur un tracker, le leecher reste `Inconnu` et la session correspondante demeure en attente. La version n'a pas été testée contre un tracker réel ni construite sur ton NAS ; le compteur JOAL des volumes annoncés reste indisponible.

## Installation / mise à jour Unraid

1. Sauvegarder ton dossier existant `data/` et `.env` (si utilisé), y compris `clients/` ; ne jamais les écraser par l'extraction.
2. Décompresser la v1.14 sur les fichiers de code dans `/mnt/user/appdata/joal-smart-controller/`.
3. Depuis le terminal Unraid :

```bash
cd /mnt/user/appdata/joal-smart-controller
docker build -t joal-smart-controller:local .
```

4. Dans **Unraid → Docker → joal-smart-controller → Edit → Apply**, recréer le conteneur et effectuer `Ctrl+F5` dans la WebUI.
5. Tester d'abord avec `dryRun: true` : importer un torrent, vérifier sa présence dans `data/torrents/`, puis vérifier son relevé et son statut dans « Bibliothèque JOAL uniquement ».

## Vérifications locales

```bash
PYTHONPATH=app:tests python3 -m unittest discover -s tests -p 'test_*.py' -q
node --check app/static/app.js
node --test tests/test_table_sort.cjs
python3 tests/browser_solo_v114.py
```

---

# JOAL Smart Controller v1.13 — Notifications, graphiques et sauvegarde persistante

Cette version conserve **la configuration v1.12** (mode Tracker + tag, priorite qBittorrent, dryRun, securite des basculements, suivi des scrapes, page de connexion, tri/pagination). Les trois ajouts ne modifient pas les conditions de selection de torrents.

## Trois nouvelles fonctions

### 1. Notifications dans la WebUI
- Bouton **Alertes** en haut de la page, badge des notifications non lues.
- Historique borne (250 evenements) des pertes/retours de connexion qBittorrent, des erreurs d'evaluation, de la surveillance des trackers et des arrets JOAL non confirmes.
- Une alerte par transition d'etat, pas une notification par cycle de 5 secondes ; marquage **Tout comme lu**.
- Enregistrees dans `/data/notifications.json` avec permissions `0600`.
- **Important : notifications uniquement dans l'interface Web ouverte.** Aucun email, push mobile, webhook ou message externe n'est configure ; aucun trafic sortant n'est emis pour les alertes.

### 2. Graphiques des leechers
- Onglet **Releves autonomes** : bouton **Graphique** pour chaque couple torrent/regle.
- Selecteur de courbe : **maximum observe pour la regle** ou **tracker particulier**. Le suivi detaille par tracker ne commence qu'apres installation de la v1.13 ; les valeurs historiques anciennes ne sont pas inventees.
- Historique sur 14 jours maximum, 1344 points par serie, avec `leechers = null` si scrape impossible. Une interruption apparait comme une coupure, jamais comme une chute a zero.
- Le module reutilise les requetes scrape HTTP(S)/UDP deja effectuees : **aucune requete supplementaire pour afficher les graphiques**.
- Les URLs d'annonce et passkeys restent dans `/data/tracker-registry.json`, jamais dans la reponse Web. Les graphiques ne prouvent pas un transfert de donnees ni un compteur accepte par un tracker.

### 3. Sauvegarde / restauration de `/data`
- Nouvel onglet **Sauvegardes** : export ZIP des donnees **persistantes** suivantes : `policy.json`, `tracker-registry.json`, `cleanup-observations.json`, `handoff-state.json`, `diagnostics.json`, `notifications.json` et `clients/*.client` (lorsqu'ils existent).
- Sont exclus : `/data/runtime` (fichiers et logs temporaires), les backups precedents, `docker-compose`/variables Docker, `.env`, les telechargements qBittorrent et les donnees multimedia.
- ZIP limite a **64 Mio** compresses / **60 Mio** decomprimes ; chemin et types de fichiers strictement valides, aucun lien symbolique ni chemin traversal accepte. Les fichiers d'origine sont reserves aux admins authentifies.
- **Restauration en deux temps** : activer `dryRun`, verifier qu'aucun JOAL n'est actif et qu'aucun torrent n'est encore sous la garde du controleur ; televerser le ZIP puis **redemarrer le conteneur**. Aucune configuration n'est remplacee a l'import : l'application de la restauration intervient au redemarrage, avant le lancement de la surveillance.
- Juste avant l'application : sauvegarde de securite `data/backups/pre-restore-*.zip`, avec conservation des trois dernieres copies. Les fichiers contenus dans le ZIP remplacent leurs homologues ; les fichiers supplementaires non inclus ne sont pas effaces.
- Par securite, apres la restauration : **dryRun est force a `true`**, le basculement automatique et le nettoyage sont desactives ; les proprietes d'anciens transferts qBittorrent ne sont pas restaurees comme actifs (`handoff-state.json` est neutralise). Verifier les regles avant de re-activer toute action.
- **La sauvegarde ZIP est confidentielle** : elle peut contenir les URL de trackers et leurs passkeys. Ne pas publier, partager ni stocker en clair sur un service non protege.

## Mise a jour sur Unraid

1. Sauvegarder les dossiers existants `data/` et le fichier `.env` (si present) **avant** d'extraire l'archive.
2. Extraire l'archive v1.13 dans `/mnt/user/appdata/joal-smart-controller/`, remplacer le code sans effacer les fichiers persistants.
3. Sur le terminal Unraid :

   ```bash
   cd /mnt/user/appdata/joal-smart-controller
   docker build -t joal-smart-controller:local .
   ```
4. Menu Unraid **Docker → joal-smart-controller → Edit → Apply**, puis `Ctrl+F5` dans la WebUI.
5. Ouvrir **Alertes**, **Releves autonomes → Graphique**, puis **Sauvegardes**. L'interface existante reste sur le port 8788.

## Tests et limitations

```bash
PYTHONPATH=app:tests python3 -m unittest discover -s tests -p 'test_*.py' -q
node --check app/static/app.js
node --test tests/test_table_sort.cjs
python3 tests/browser_v113.py  # necessite Playwright et Chromium
```

Tests automatiques : notification, autorisations HTTP et CSRF, rejet ZIP dangereux, staging/restauration, series historiques et session navigateur. **Pas de construction Docker ni de test reseau sur ton NAS dans cet environnement** : tester d'abord en mode `dryRun`.

---

# JOAL Smart Controller v1.12 — Diagnostics, historique et connexion Web

Cette version conserve les règles, le filtrage **Tracker + tag**, la priorité à qBittorrent et les protections de la v1.11.6. Les nouvelles fonctions sont **en lecture seule**, à l'exception de l'authentification et de la restauration de configuration explicitement confirmée.

## Nouveautés

- **Page de connexion moderne** : remplace l'authentification HTTP Basic du navigateur. Nom d'utilisateur : `admin`, mot de passe : variable d'environnement `ADMIN_PASSWORD` (12 caractères minimum). Cookie de session `HttpOnly; SameSite=Strict`, durée maximale de 24 h, expiration après 8 h sans activité, protection CSRF sur les commandes, déconnexion et limitation des tentatives répétées. Les sessions expirent aussi au redémarrage du conteneur.
- **Diagnostics & journal** : tableau de l'état qBittorrent, nombre de processus JOAL actifs, âge du dernier inventaire, horodatage du dernier relevé tracker, événements et sessions. Les événements sont conservés dans `/data/diagnostics.json`, limité à 1500 événements et 400 sessions, avec permissions `0600`.
- **Historique par torrent** : bouton **Historique** dans la dernière colonne de « Torrents suivis » ; affiche événements et sessions par infohash.
- **Export diagnostic** : bouton **Exporter JSON** dans Diagnostics, sans URL privée de tracker ni mot de passe.
- **Sauvegarde / restauration des règles** : dans **JSON avancé**, export et réimport de `policy.json` (confirmation obligatoire). Ce n'est **pas** une sauvegarde complète du volume `/data`, du registre des trackers ou des profils JOAL.
- **Comptage honnête** : le volume uploadé par qBittorrent reste distinct du compteur JOAL, **non mesuré**. Le moteur JOAL embarqué ici n'expose pas encore de compteur cumulatif fiable par torrent. Aucun débit ou volume inventé n'est affiché et le contrôleur ne suppose pas qu'un volume simulé a été accepté par un tracker.

## Mise à jour sur Unraid

1. **Sauvegarder** les dossiers `data/` et le fichier `.env` (si utilisation Docker Compose). Conserver les réglages Docker dans Unraid si installation par template.
2. Décompresser la v1.12 par-dessus le dossier de code **sans supprimer `data/` ni `.env`**.
3. Dans le terminal Unraid :

   ```bash
   cd /mnt/user/appdata/joal-smart-controller
   docker build -t joal-smart-controller:local .
   ```

4. Dans **Docker → joal-smart-controller → Edit → Apply**, recréer le conteneur, puis actualiser le navigateur avec `Ctrl+F5`.
5. L'adresse reste `http://IP_UNRAID:8788` (ou ton port personnalisé). Tu arrives sur **Connexion** : utilisateur `admin` ; mot de passe `ADMIN_PASSWORD` déjà configuré. Les anciens identifiants HTTP Basic stockés par le navigateur ne sont plus utilisés.
6. Tester **Diagnostics & journal**, puis cliquer sur **Historique** d'un torrent. Les sessions antérieures à la v1.12 ne peuvent pas être reconstituées.

### Sécurité du navigateur et du réseau

- Par défaut, les cookies sont conçus pour un **accès HTTP local de confiance**. Ne pas exposer le port 8788 directement sur Internet.
- Avec un reverse proxy fournissant HTTPS, définir `COOKIE_SECURE=1` **dans le template Unraid (Show more settings)** ou `.env` puis recréer le conteneur. En HTTP simple, laisser `0` : sinon le navigateur ne transmettra pas le cookie et l'utilisateur ne pourra pas se connecter.
- Limiter le réseau/les IP autorisées via Unraid ou le reverse proxy. Une page de connexion n'est pas un substitut à HTTPS pour la confidentialité du mot de passe.
- Le mot de passe reste défini dans `ADMIN_PASSWORD` ; ne pas le mettre dans `policy.json` ni dans les exports.
- Les API JSON de modification nécessitent une session active, un jeton CSRF et une requête d'origine autorisée ; les requêtes sans session reçoivent un JSON `401` sans en-tête `WWW-Authenticate: Basic`.

### Vérifications locales

```bash
PYTHONPATH=app:tests python3 -m unittest discover -s tests -p 'test_*.py' -q
node --check app/static/app.js
node --check app/static/login.js
node --test tests/test_table_sort.cjs
python3 tests/browser_smoke.py        # Chromium + Playwright requis
python3 tests/browser_visual_v112.py # idem, pages locales simulées
```

Les tests du navigateur servent des données simulées ; la connexion réelle à qBittorrent, les trackers et l'exécution Java ne sont pas couvertes. Les navigateurs de l'environnement de test peuvent interdire l'accès HTTP aux serveurs de test locaux.

---

# JOAL Smart Controller v1.11.6 — seuil à confirmation accélérée

**Changement :** avec une règle fixant le minimum à `N`, un relevé autonome **frais et techniquement valide** indiquant **au moins `N + 5` leechers** peut suffire pour valider immédiatement le seuil. Par exemple, minimum 4 ⇒ 9 leechers ou plus : **une seule mesure**. Entre 4 et 8 leechers : les **2 confirmations** (ou le nombre configuré) restent requises.

Le supplément `+5` est réglable sous **Paramètres → Marge pour validation immédiate** (`handoff.fastTrackMargin`). Il s’applique à chaque seuil de règle, y compris les exceptions par infohash.

Un relevé *scrape partiel* peut être retenu **pour les trackers ayant effectivement répondu**, avec un compteur de provenance. Il n'est jamais traité comme une mesure globale de tous les trackers. Une absence de réponse, une valeur en cache qBittorrent ou un relevé périmé **ne valident jamais** le seuil. Les opérations destructives de nettoyage gardent leurs protections précédentes.

Le raccourci ne supprime **aucune autre condition** : priorité à qBittorrent, torrent complet, tag, tracker ciblé, capacités, temporisation de 60 minutes après retour, absence de conflit et arrêt vérifié. Le nombre de confirmations pour le retour sous qBittorrent n'est pas réduit par cette règle.

**Mise à jour :** sauvegarder `data/` et `.env`; remplacer les fichiers du projet, puis exécuter `docker build -t joal-smart-controller:local .` dans le dossier, et appliquer **Docker → Edit → Apply** dans Unraid. Les configurations préexistantes reçoivent la marge par défaut de 5 sans modifier `policy.json` manuellement. Valider d'abord en `dryRun: true`.

---

# JOAL Smart Controller v1.11.4 — Leechers HTTP(S)/UDP en lecture seule

- Le module de surveillance sait maintenant utiliser le **scrape UDP standard (BEP 15)** en plus du scrape HTTP(S) existant, sans émettre d'annonce BitTorrent ni prétendre envoyer des données.
- Vérification de la réponse UDP : transaction, action, infohash v1, longueur et destination publique ; les échecs restent `Inconnu` et ne deviennent jamais zéro.
- Si plusieurs trackers sont configurés, un résultat **partiel** peut être affiché (nombre de réponses réussies / trackers interrogés), mais ne sert jamais à autoriser un basculement ou un nettoyage. La liste des trackers est resynchronisée au maximum une fois par heure lorsque qBittorrent est accessible.
- Les chiffres déjà connus de qBittorrent peuvent être affichés même sans relevé autonome, avec la mention **« qBit non daté (non validé) »**. Ils sont **informatifs uniquement** : pas de basculement ou de suppression fondé sur ce secours.
- Tous les noms des trackers restent cliquables si leur domaine ne correspond à aucune règle, avec fenêtre de sélection de la règle.
- Les passkeys et URL privées sont conservées uniquement dans le registre local protégé, et non exposées dans le tableau.
- Un tracker UDP privé peut refuser un scrape ou ne pas offrir ce service ; la présence d'un chiffre dans qBittorrent via `announce` ne garantit pas la possibilité de lecture indépendante.

## Installation

Conserver `.env` et `data/`, remplacer les fichiers de code et reconstruire l'image `joal-smart-controller:local`. Puis recréer le conteneur via **Docker → Edit → Apply** et recharger la WebUI.

## Tests

`python3 -m unittest discover -s tests -q` et `node --test tests/test_table_sort.cjs` ; tests UDP déterministes avec de faux sockets, sans contacter de trackers réels.

Les sections historiques ci-dessous décrivent les anciennes versions ; les nouveautés v1.11.4 ci-dessus remplacent leurs limitations UDP.

---

# JOAL Smart Controller v1.11.3 — Domaines de trackers cliquables

Cette mise à jour améliore uniquement le tableau de bord et l’ajout des domaines aux règles. Elle n’ajoute pas de mécanisme de réponse ou de dissimulation BitTorrent et ne modifie pas les conditions de sélection ni les opérations de basculement.

## Changements

- Le **nom complet** de tout tracker non couvert est cliquable, y compris un domaine UDP ; la fenêtre indique explicitement que le scrape autonome UDP n’est pas disponible.
- Une légende indique les couleurs : **vert** = domaine couvert par une règle active ; **ambre** = domaine couvert par une règle désactivée ; **bleu avec +** = domaine absent de toutes les règles.
- Cliquer sur un nom ouvre une **fenêtre de sélection de règle** ; le domaine cliqué est précoché. Les autres domaines du même torrent sont proposés non cochés, permettant une attribution multiple intentionnelle.
- La liste déroulante propose les règles existantes et présélectionne la règle correspondante au torrent si possible.
- Les données transmises ne contiennent que les domaines, jamais les URL de tracker ni les passkeys.
- Les scrapes autonomes HTTP(S) restent distincts des valeurs reçues par qBittorrent via des announces : l’absence de scrape n’implique pas que qBittorrent ne dispose d’aucun compteur, mais un compteur ancien ne remplace pas un relevé frais.

## Vérification

```bash
python3 -m unittest discover -s tests -q
node --test tests/test_table_sort.cjs
node --check app/static/app.js
python3 tests/browser_v112.py
python3 tests/browser_v113.py
```

## Mise à jour Unraid

Sauvegarder `data/` et `.env`, puis remplacer les fichiers du projet avec ceux de l’archive, sans écraser `data/`. Ensuite :

```bash
cd /mnt/user/appdata/joal-smart-controller
docker build -t joal-smart-controller:local .
```

Dans l’interface Unraid : **Docker → joal-smart-controller → Edit → Apply**. Recharger la WebUI avec **Ctrl+F5**.

## Limites

- Un tracker UDP peut être autorisé dans une règle, mais la collecte autonome `scrape` implémentée dans l’application reste HTTP(S) uniquement.
- Aucune synchronisation avec qBittorrent réel, tracker public ou Docker sur NAS n’est incluse dans les tests de ce paquet.

---

# JOAL Smart Controller v1.11.2 — Explication des exclusions et gestion des trackers

Cette version améliore le **diagnostic visuel** dans « Torrents suivis », sans modifier les critères d'éligibilité, le seuil de leechers, les basculements ni les limites JOAL/qBittorrent.

## Vérifications visibles au survol

- Survoler ou sélectionner au clavier le statut **Exclu**, **À vérifier**, **Retenu**, etc. ouvre une infobulle donnant le **motif actuel** et une liste de conditions : tag de surveillance, règle active, domaine correspondant, torrent complet et arrêté dans qBittorrent, disponibilité des données de leechers, seuil applicable (y compris exception par infohash), ratio local, temporisation et suppression en cours.
- Les conditions sont différenciées : **✓ satisfaite**, **✕ non satisfaite**, **? non vérifiable**. « Nombre inconnu » ne signifie jamais zéro.
- La provenance des leechers est affichée lorsqu'elle est connue. L'infobulle indique également la date de la dernière évaluation.
- Si plusieurs règles existent, le tableau privilégie désormais la règle ayant effectivement un domaine de tracker correspondant, et non la première règle sans rapport.

## Affichage et ajout des trackers

- Tous les domaines de trackers identifiables sont affichés pour chaque torrent : les domaines HTTP(S) peuvent être associés à des règles, les trackers UDP restent visibles avec mention de la limite de surveillance.
- Un **domaine HTTP(S) absent de toutes les règles** apparaît comme un bouton **« + domaine »**. Cliquer dessus ouvre une fenêtre qui propose **tous les domaines HTTP(S) manquants du même torrent**, précochés, avec sélection de la règle destinataire. L'ajout est enregistré explicitement après validation ; aucune nouvelle règle n'est activée automatiquement.
- Un sous-domaine couvert par un domaine parent, par exemple `bt4.t-ru.org` déjà couvert par `t-ru.org`, est signalé comme reconnu et n'est pas ajouté en double.
- **Confidentialité** : seule la partie domaine est renvoyée à l'interface, sans chemin de tracker, passkey ni URL complète. La validation existante interdit les recouvrements entre domaines de règles actives et limite une règle à 20 domaines.

## Vérifications de la v1.11.2

```bash
python3 -m unittest discover -s tests -q
node --test tests/test_table_sort.cjs
node --check app/static/app.js
python3 tests/browser_smoke.py
python3 tests/browser_v112.py
```

Ces contrôles utilisent des trackers et torrents simulés ; ils ne valident ni des annonces JOAL réelles, ni l'installation finale sur un serveur Unraid.

## Mise à jour Unraid

1. Sauvegarder le dossier `data/` et `.env`.
2. Extraire les fichiers de cette version dans `/mnt/user/appdata/joal-smart-controller/`, en conservant les données persistantes.
3. Construire et réappliquer le conteneur :

   ```bash
   cd /mnt/user/appdata/joal-smart-controller
   docker build -t joal-smart-controller:local .
   ```

4. Dans **Unraid → Docker → joal-smart-controller → Edit → Apply**. Recharge ensuite la WebUI avec **Ctrl+F5**.

---

# JOAL Smart Controller v1.11.1 — Tableau à colonnes ajustables

Cette mise à jour modifie **uniquement l'interface** de la v1.11 : le backend, les règles de sélection et les protections de basculement sont inchangés.

## Onglet « Torrents suivis »

- Redimensionne chaque colonne en **glissant la fine poignée verticale** au bord droit de son en-tête. Les touches fléchées fonctionnent également lorsque la poignée est sélectionnée (20 px par pression).
- **Double-clic sur la poignée** pour restaurer une colonne, ou bouton **↺ Colonnes** pour restaurer toutes les largeurs.
- Les largeurs sont conservées **dans le navigateur** avec `localStorage`. Elles sont indépendantes des paramètres serveur et ne sont pas synchronisées entre navigateurs.
- Une **barre de défilement horizontale** et des boutons **◀ / ▶** apparaissent sous le tableau si ses colonnes dépassent la largeur de l'écran. La barre reste visible au bas de la fenêtre pendant la lecture d'une longue liste.
- Les lignes longues sont tronquées visuellement. Le survol des cellules révèle leur contenu complet.
- Tri, filtres, pagination 10/20/50/100/500, boutons de basculement et rafraîchissement automatique inchangés.

## Mise à jour Unraid

1. Sauvegarder `data/` et `.env`. Extraire cette archive dans `/mnt/user/appdata/joal-smart-controller/`, sans supprimer ni écraser les données de configuration persistantes.
2. Reconstruire l'image :

   ```bash
   cd /mnt/user/appdata/joal-smart-controller
   docker build -t joal-smart-controller:local .
   ```

3. Dans Unraid, **Docker → joal-smart-controller → Edit → Apply**, puis ouvrir la WebUI et faire `Ctrl+F5`.

## Vérifications

```bash
python3 -m unittest discover -s tests -q
node --test tests/test_table_sort.cjs
node --check app/static/app.js
python3 tests/browser_smoke.py
```

Le test Chromium couvre une liste de 57 torrents, le tri, les filtres, la pagination, le redimensionnement à la souris et au clavier, la mémorisation des tailles, la synchronisation des barres et le positionnement de la barre secondaire pendant le défilement vertical. Aucun essai sur Unraid réel n'est effectué ici.

---

# JOAL Smart Controller v1.11 — Basculement manuel par torrent

La colonne **Basculement manuel** est maintenant disponible dans **Torrents suivis**.
Chaque torrent tagué et complet possède deux boutons :

- **Vers JOAL** : enregistre une demande prioritaire (et non un passage immédiat garanti). Le contrôleur conserve les vérifications : domaine et règle autorisés, relevés `scrape` récents, minimum de leechers propre à la règle/torrent, au moins deux observations distinctes, capacités globales et par règle, état complet et sûr de qBittorrent, délai minimum de 60 minutes. Il arrête qBittorrent uniquement après ces contrôles et confirme la demande uniquement lorsque le processus JOAL correspondant tourne réellement. Une mesure inconnue ne devient jamais zéro.
- **Vers qBittorrent** : demande le retour prioritaire vers le partage réel. Si JOAL héberge le torrent, son processus est arrêté **avant** le démarrage de qBittorrent. L'état et la complétude du torrent sont vérifiés. Une demande explicite peut démarrer un torrent précédemment arrêté manuellement ; un clic de confirmation est donc requis. La temporisation anti-basculement de 60 minutes minimum s'applique ensuite.

Une action est **enregistrée et traitée par le worker**. Les états visibles sont « En attente », « Terminé » et « Refusé ». Une demande JOAL expire après 24 h si les conditions n'ont pas été réunies. La dernière demande remplace la précédente ; une demande vers qBittorrent est prioritaire et annule une demande JOAL en attente. Les commandes sont désactivées en mode `dryRun`.

### Limitations importantes

- **JOAL est actuellement exécuté par processus partagé au niveau de la règle**. Un retour manuel d'un seul torrent peut nécessiter d'arrêter temporairement le processus associé, ce qui interrompt aussi les autres torrents de cette règle. Le contrôleur essaye ensuite de les resélectionner, dans la limite du délai anti-redémarrage existant (15 minutes). En cas d'incertitude, retour prioritaire au partage réel.
- Le bouton JOAL ne permet pas de contourner le seuil de leechers, l'indisponibilité d'un tracker, un arrêt manuel ultérieur de qBittorrent, une protection contre le double démarrage, ni une période de refroidissement.
- Les profils `.client` et l'intégration JOAL sont inchangés. Aucune fonction de dissimulation de client ou de falsification de preuve de transfert n'a été ajoutée.
- L'API qBittorrent doit rester disponible pour toute action et la surveillance de sécurité.

### Mise à niveau depuis v1.10 (Unraid)

Sauvegarder `/mnt/user/appdata/joal-smart-controller/data/` et `.env`, puis remplacer les fichiers du projet sans supprimer les données. Exécuter :

```sh
cd /mnt/user/appdata/joal-smart-controller
docker build -t joal-smart-controller:local .
```

Dans **Unraid → Docker → joal-smart-controller → Edit → Apply**, recréer le conteneur. Recharger la WebUI avec `Ctrl+F5`. Les paramètres actuels sont préservés ; le fichier `/data/handoff-state.json` conserve également les demandes manuelles.

**Précondition** : pour demander JOAL, activer le pilotage automatique dans Paramètres et désactiver `dryRun` après validation des règles. Pour l'essai initial, garder `dryRun=true`, vérifier les boutons (désactivés) et le rendu des états, puis ne lancer un changement réel que sur un torrent choisi consciemment.

### Vérifications

`python3 -m unittest discover -s tests -q` : tests unitaires et HTTP avec faux clients qBittorrent et faux scrapes.
`python3 tests/browser_smoke.py` : interface Chromium simulée (57 torrents) si Playwright est installé.
L'exploitation réelle sur Unraid et trackers privés n'est pas testée ici. Le passage vers JOAL n'implique pas que ses annonces soient acceptées par le tracker.

---

# JOAL Smart Controller v1.10.0 — qBittorrent prioritaire et anti-oscillation

## Nouveau dans cette version

- **qBittorrent reste le gestionnaire principal** : tout torrent qui démarre dans qBittorrent suspend JOAL via la garde rapide (5 secondes par défaut). Les états inconnus ou la perte de connexion entraînent l'arrêt préventif des processus JOAL.
- **Basculement automatique facultatif** : nouveau réglage sous **Paramètres → Priorité à qBittorrent et anti-basculement**. Il est **désactivé par défaut**, même après mise à jour depuis v1.9.1. Un `dryRun: true` interdit également les opérations automatiques.
- Si activé, un torrent complet et réellement actif dans qBittorrent ne peut être automatiquement arrêté qu'après **2 scrapes frais distincts** (configurable entre 2 et 5), au-dessus du seuil correspondant à sa règle ou à son infohash, dans les limites globales et par règle. Les compteurs qBittorrent en cache ne sont pas acceptés pour cet arrêt.
- **Retour vers qBittorrent** : lorsque les conditions ne sont plus sûres ou qu'une reprise de qBittorrent est détectée, priorité à l'arrêt de JOAL. Un torrent arrêté **par le contrôleur lui-même** peut être redémarré automatiquement dans qBittorrent ; un torrent arrêté manuellement ne l'est **jamais**.
- **Temporisation anti-oscillation** : après tout retour vers qBittorrent, au moins **60 minutes** avant un nouveau basculement, y compris après redémarrage du conteneur. Le délai est réglable de 60 minutes à 7 jours.
- L'état du basculement, les dernières mesures et la temporisation sont enregistrés dans `data/handoff-state.json`, avec permissions 0600, et affichés dans la colonne **Motif** du tableau.
- Le système conserve les 57+ torrents surveillés, le classement global par leechers, les 10 sessions maximum (si `maxCandidates=10`), le tri et la pagination de v1.9.1.

### Ce que cette version ne garantit pas

Le contrôle rapide de qBittorrent et les interrogations des trackers sont **deux fréquences distinctes**. Par défaut, la garde qBittorrent intervient toutes les 5 s, l'évaluation des sélections toutes les 180 s, et les relevés `scrape` au plus tôt toutes les 15 min par torrent et par règle. Avec 57 torrents et 20 scrapes au maximum par cycle, plusieurs cycles peuvent être nécessaires pour produire 2 relevés consécutifs. Les trackers UDP ou qui n'offrent pas de scrape ne peuvent pas autoriser d'arrêt automatique.

JOAL fonctionne comme un processus séparé par règle. Il peut avoir une limite anti-relance de 15 minutes. Si un moteur JOAL ne démarre pas, le contrôleur tente de rendre la priorité à qBittorrent et déclenche la temporisation. Les états réels du tracker et la réussite des transferts ne sont pas prouvés par ces tests. Le système ne rend pas les annonces fictives indétectables et n'ajoute aucun mécanisme de dissimulation.

### Mise à jour Unraid

1. Sauvegarder `data/` et `.env`, surtout `data/policy.json` et `data/tracker-registry.json`.
2. Décompresser l'archive v1.10.0 par-dessus la version installée dans `/mnt/user/appdata/joal-smart-controller/`, sans remplacer `data/` ou `.env`.
3. Dans le terminal Unraid :

   ```bash
   cd /mnt/user/appdata/joal-smart-controller
   docker build -t joal-smart-controller:local .
   ```

4. Dans **Docker → joal-smart-controller → Edit → Apply**, recréer le conteneur, puis recharger la WebUI avec `Ctrl+F5`.
5. Dans **Paramètres**, laisser **Simulation locale (dryRun)** activée pour vérifier l'interface. Le basculement automatique reste désactivé tant que vous ne l'activez pas explicitement.

### Tests

```bash
python3 -m unittest discover -s tests -q
node --test tests/test_table_sort.cjs
node --check app/static/app.js
python3 tests/browser_smoke.py
```

Les tests utilisent des clients qBittorrent et des réponses de tracker simulés. Aucun basculement vers JOAL sur un tracker réel, ni déploiement Docker/Unraid de bout en bout, n'a été vérifié ici.

---

# JOAL Smart Controller v1.9.1 — Tri et pagination des torrents

Cette mise à jour modifie uniquement le **tableau des torrents** et fournit la date d’ajout dans l’inventaire.

- Cliquer sur les en-têtes pour trier par **nom, domaine du tracker, règle, leechers, ratio local, date d’ajout, état ou motif**. Un deuxième clic inverse le sens. Le tri du ratio porte sur le **ratio local qBittorrent**, et non le ratio cible.
- Les nombres sont triés comme des nombres, les dates sont triées chronologiquement. Les valeurs « Inconnu » et les dates manquantes restent en dernière position dans les deux sens.
- Choisir **10, 20, 50, 100 ou 500 lignes par page** (100 par défaut), puis naviguer avec **Précédent / Suivant**. Le tri et les filtres s’appliquent à l’ensemble des torrents reçus de l’API **avant** la pagination.
- L’API qBittorrent fournit `added_on` (horodatage Unix) : la date est affichée dans une nouvelle colonne lorsqu’elle est renseignée, sinon `—`.
- Le choix du tri et de la page est conservé pendant les actualisations automatiques toutes les 5 secondes ; changer un filtre, le tri ou la taille de page retourne à la première page. En cas de diminution du nombre de torrents, la page courante est ajustée automatiquement.
- Le plafond `maxCandidates` et les seuils par règle ou par torrent sont **inchangés**. La pagination est purement visuelle et n’affecte pas les décisions du contrôleur.
- La limite d’inventaire serveur préexistante de 1 500 lignes est inchangée ; un avertissement est affiché si l’API a écarté des lignes. Aucun téléchargement ou transfert n'est déclenché par le tri.

## Mise à jour Unraid

1. Sauvegarder le dossier `data/` et le fichier `.env`.
2. Décompresser l’archive à la place de l’installation précédente, **sans supprimer `data/` ni `.env`**.
3. Dans `/mnt/user/appdata/joal-smart-controller`, lancer `docker build -t joal-smart-controller:local .`.
4. Dans **Unraid → Docker → joal-smart-controller → Edit → Apply**, recréer le conteneur, puis `Ctrl+F5` dans sa WebUI.

## Tests v1.9.1

- `python3 -m unittest discover -s tests -q` : API, paramètres, affichage HTML, date d’ajout.
- `node --test tests/test_table_sort.cjs` : tailles de page, navigation, tri numérique, dates, valeurs inconnues, combinaison filtres / tri / pagination.
- `node --check app/static/app.js` et `node --check app/static/torrent_table.js` : syntaxe JavaScript.
- `python3 tests/browser_smoke.py` (facultatif, si Chromium et Playwright sont disponibles) : test réel des clics et de la pagination avec 57 torrents simulés.

Tests réalisés avec clients simulés uniquement. Pas de déploiement réel sur Unraid pendant la création de cette archive.

---

# JOAL Smart Controller v1.9 — Inventaire complet, classement des torrents

- `maxCandidates` est conservé pour compatibilité avec les configurations existantes, mais il représente maintenant le **nombre maximum de torrents retenus simultanément** à l’échelle de toutes les règles. Il ne bloque plus l’évaluation des torrents tagués.
- Ex. 57 torrents tagués, `maxCandidates=10` : les 57 sont examinés, les 10 premiers qui satisfont leurs règles et quotas sont retenus, triés par le nombre de leechers décroissant.
- Le seuil n’est jamais fixé à 4 : chaque règle porte son propre `minLeechers`. Une liste facultative `minLeechersByHash` peut substituer un seuil spécifique à un torrent de cette même règle. Les valeurs sont relues à chaque cycle.
- Les torrents restants affichent le seuil qui leur est applicable, leur rang et les motifs de non-sélection (trop peu de leechers, quota de règle, limite globale, état qBittorrent, tracker inconnu).
- Lorsque disponible, un relevé `scrape` autonome récent est préféré pour le nombre de leechers et le classement ; sinon les chiffres `num_leeches` de qBittorrent peuvent être périmés. Le démarrage reste interdit si le torrent n’est pas terminé et arrêté dans qBittorrent.
- Aucun changement de l’API qBittorrent, des profils `.client`, ni du port TCP. Nettoyage des données toujours désactivé par défaut.

## Mise à jour Unraid

Sauvegarder `data/` et `.env`, décompresser l’archive au même emplacement, exécuter `docker build -t joal-smart-controller:local .` depuis le dossier de l’application, puis dans Docker → Edit → Apply. La valeur de `maxCandidates` existante reste conservée : définir **10** dans Paramètres pour limiter les sélections globales à dix.

## Tests

`python3 -m unittest discover -s tests -q` (tests simulés uniquement ; vérification NAS réelle nécessaire).

---

# JOAL Smart Controller v1.8 — Surveillance autonome des trackers

## Principe

Cette version rend **les observations des trackers** autonomes après une première
synchronisation. Quand qBittorrent signale un torrent portant le tag `joal`, le
contrôleur mémorise l'infohash et les URLs de trackers associées ; un composant
séparé effectue ensuite des requêtes de **scrape HTTP(S) en lecture seule**. Un
tracker compatible peut renvoyer le nombre de leechers (`incomplete`) même si
le torrent est arrêté dans qBittorrent. Un zéro est affiché uniquement lorsqu'un
scrape récent a réellement renvoyé zéro. Les échecs et lectures trop anciennes
sont affichés comme **Inconnu**, jamais comme zéro.

**Limite essentielle** : cette autonomie concerne seulement la **surveillance**.
qBittorrent reste nécessaire pour le tag d'admission, la vérification que le
torrent est complet/arrêté, les opérations de gestion, la suppression éventuelle
et la garde contre deux sessions simultanées. Si qBittorrent devient inaccessible,
la garde continue de stopper JOAL par précaution ; les scrapes peuvent continuer
à titre informatif. Cette version ne rend pas les annonces fictives autonomes et
ne modifie pas le moteur JOAL, les volumes annoncés ni son protocole.

## Modifications v1.8

- Nouvel onglet **Relevés autonomes** : nom, domaine, règle, leechers, date du
  dernier relevé et qualité (`récent`, `périmé`, `indisponible`).
- Nouvelle section dans **Paramètres** : activation de la surveillance,
  intervalle minimal entre les scrapes (15 minutes par défaut), âge maximal
  d'une lecture (45 minutes par défaut).
- Inventaire persistant dans `/data/tracker-registry.json`, créé avec permissions
  `0600`, et utilisable même lorsque l'API qBittorrent tombe temporairement.
  **Les URLs des trackers peuvent contenir des passkeys** : protéger le dossier
  `data/` et ses sauvegardes ; ne jamais publier ce fichier.
- Une nouvelle règle activée peut être suivie sans réimporter le `.torrent`,
  car les URLs déjà collectées restent dans le registre. Un torrent dont le tag
  est retiré est retiré du registre lors de la synchronisation suivante.
- Les appels au tracker sont limités à 20 contrôles torrent/règle par cycle.
  Seuls les domaines configurés dans une règle active peuvent être interrogés.
- Les données privées ne figurent jamais dans `/api/state` : seul le domaine
  est transmis au navigateur.
- Le contrôle qBittorrent toutes les `guardSeconds` reste séparé des requêtes
  de collecte, afin que la latence d'un tracker ne bloque pas la garde.
- Les options historiques, l'import `.torrent`, la visualisation, `Tracker + tag`
  et le template Docker Unraid sont conservés. Le suivi du nettoyage conserve
  ses propres vérifications fraîches juste avant une action ; **la suppression
  automatique des données demeure désactivée par défaut**.

### Limites de compatibilité

- L'API qBittorrent doit répondre au moins une fois pour que les métadonnées
  du torrent soient collectées. Le registre ne constitue pas une bibliothèque
  BitTorrent complète et ne remplace pas les données téléchargées.
- Les trackers UDP et les trackers privés désactivant `scrape` ne fournissent
  aucun relevé indépendant. Les torrents uniquement en BitTorrent v2 (infohash
  de 64 caractères) ne sont pas pris en charge par ce module.
- Les règles de sélection JOAL préexistantes continuent d'utiliser leur
  contrôle qBittorrent ; l'onglet autonome est un tableau d'observation et
  **pas un mécanisme permettant d'annoncer des transferts fictifs**.
- Un scrape est une photographie ponctuelle, pas une garantie que les mêmes
  leechers sont encore présents plusieurs minutes plus tard.

### Mise à niveau depuis v1.7 / v1.7.1 sur Unraid

1. Sauvegarder `data/` et `.env` ; **ne pas supprimer** `data/policy.json`.
2. Extraire la v1.8 dans `/mnt/user/appdata/joal-smart-controller/` en remplaçant
   le code, sans effacer `data/` ni `.env`.
3. Dans un terminal Unraid :

   ```sh
   cd /mnt/user/appdata/joal-smart-controller
   docker build -t joal-smart-controller:local .
   ```

4. **Docker → joal-smart-controller → Edit → Apply**, puis WebUI et `Ctrl+F5`.
5. Vérifier l'onglet **Relevés autonomes** après synchronisation. Aucun changement
   de mot de passe, de réseau Docker ou de volume n'est requis.

### Tests

`python3 -m unittest discover -s tests -q` exécute les tests unitaires.
Les tests de scrapes utilisent de faux trackers : **aucun tracker réel n'est
contacté** pendant les tests. Un test complet avec qBittorrent 5.3.0/Unraid
réels reste nécessaire.

---

# JOAL Smart Controller v1.7.1 — Diagnostic de règles et trackers

## Correctif v1.7.1

- Les domaines des trackers HTTP(S) sont désormais affichés **même lorsque toutes les règles sont désactivées**. Seul le domaine apparaît, jamais l'URL ni la passkey.
- Si une règle désactivée correspond au domaine du torrent, le tableau indique son identifiant, l'état **Règle inactive** et le motif précis : « Règle « ... » désactivée — activer dans Règles & trackers ».
- Les leechers affichent **0** uniquement lorsque qBittorrent retourne un relevé numérique exploitable avec un tracker marqué opérationnel (`status=2`). Sinon, le tableau affiche **Inconnu** ; cela ne veut pas dire zéro et peut refléter l'absence de statistiques récentes pour un torrent arrêté.
- Si aucune règle n'est configurée, le motif invite à en créer une ; si aucune ne correspond au domaine, le diagnostic le signale distinctement.
- Aucune modification de la logique de simulation JOAL, des restrictions « Tracker + tag », de la suppression ou de l'API qBittorrent.

### Diagnostic sans afficher de secret

```sh
docker exec joal-smart-controller python3 -c 'import json; c=json.load(open("/data/policy.json")); print([(r["id"], r["enabled"], r["trackerDomains"]) for r in c["rules"]])'
```

Les règles de l'exemple sont **désactivées par défaut** (`enabled=false`). Ouvrir **Règles & trackers**, activer la règle correcte puis cliquer **Enregistrer et appliquer**.

---

# JOAL Smart Controller v1.7 — Motifs d'exclusion et nettoyage contrôlé

## Nouveautés v1.7

- **Motif d'exclusion explicite** dans l'onglet **Torrents suivis** : par exemple
  `Leechers insuffisants : 0 / minimum 3`. Les torrents tagués restent affichés
  même s'ils sont actifs, incomplets ou bloqués.
- **Nouvel onglet « Nettoyage & historique »** : relevés d'absence de leechers,
  début de la période à zéro, temps restant, statut et motif d'un contrôle manquant.
- **Réglages visuels** dans **Paramètres → Nettoyage automatique** : activation
  du suivi, durée de 3 à 365 jours, fréquence de vérification de 30 à 1440 min,
  autorisation distincte de suppression du torrent **avec ses données**.
- Nettoyage **désactivé par défaut**. Activer le suivi ne supprime rien.
  Même avec « Supprimer automatiquement » coché, aucune suppression n'est faite
  lorsque `dryRun=true` (prévisualisation). Une suppression exige donc
  `cleanup.enabled=true`, `cleanup.deleteData=true` et `dryRun=false`.
- Historique persistant dans `/data/cleanup-observations.json`. Ce fichier est
  créé par le programme ; ne pas le supprimer durant la mise à jour.

## Quelle donnée garantit « zéro leecher depuis trois jours » ?

L'API qBittorrent `torrents/trackers` expose le champ `num_leeches`, mais **ne
prouve pas sa fraîcheur pour un torrent arrêté**. Ces chiffres peuvent être en
cache. La v1.7 n'utilise donc **pas** ces valeurs pour autoriser une suppression.

Lorsque le suivi est activé, le contrôleur interroge périodiquement les trackers
HTTP(S) **autorisés** par un **scrape standard (BEP 48)**, qui retourne un
compteur `incomplete`. Il faut une réponse récente réussie **de tous** les
trackers concernés et un nombre égal à zéro. Les autres situations remettent
le compteur à zéro ou laissent le torrent « à vérifier » : torrent activé,
tracker injoignable, données malformées, leecher détecté, changement de
configuration des trackers, redémarrage interrompant trop longtemps la série.
Un minimum de quatre relevés et une période d'au moins `afterDays` jours sont
exigés. Avant suppression, qBittorrent est reconsulté et un autre scrape est
réalisé. Le programme arrête également tout processus JOAL affecté avant de
demander l'effacement via `torrents/delete` avec `deleteFiles=true`.

**Limites importantes :** les trackers privés peuvent refuser les scrapes, les
trackers UDP ne sont pas pris en charge, les torrents purement BitTorrent v2
sans infohash v1 ne sont pas pris en charge par ce contrôle. En cas de doute ou
d'échec, **aucun fichier n'est supprimé**. Les scrapes sont plafonnés à 60
Torrents par cycle ; un trop grand nombre de torrents peut être laissé sans
historique continu. Le contrôle fonctionne uniquement sur les torrents **avec
le tag `joal`, complets et arrêtés**, et les trackers correspondant aux règles
activées. Un torrent déjà passé à `JoalCompleted` est exclu du nettoyage.

Une suppression `deleteFiles=true` est définitive et peut affecter des données
partagées entre torrents. **Sauvegarder d'abord**. Le compteur de suppression
affiché représente les demandes d'effacement acceptées par l'API, pas un audit
du système de fichiers. Le projet n'a pas été exécuté sur un tracker privé
réel ou un NAS Unraid réel dans l'environnement de génération.

## Mise à niveau depuis v1.6 sur Unraid

1. Sauvegarder `data/` (en particulier `data/policy.json`) et `.env`.
2. Extraire l'archive sur `/mnt/user/appdata/joal-smart-controller/` **sans
   écraser** `data/` ni `.env`.
3. Exécuter sur le terminal Unraid :

   ```sh
   cd /mnt/user/appdata/joal-smart-controller
   docker build -t joal-smart-controller:local .
   ```

4. Dans **Docker → joal-smart-controller → Edit → Apply** pour recréer le conteneur.
5. Forcer le rafraîchissement WebUI (`Ctrl+F5`). Laisser le nettoyage désactivé
   pendant la vérification du tableau, puis activer le **suivi seul** pour voir
   si les trackers prennent en charge les scrapes.

Les paramètres et l'inventaire qBittorrent/JOAL des versions précédentes sont
conservés ; le mode de sélection reste **Tracker + tag**.

## Vérification technique

- Lancer `python3 -m unittest discover -s tests -v` depuis la racine du projet.
- Syntaxe Python, JavaScript, intégrité ZIP et XML vérifiées avant livraison.
- Les tests utilisent des clients simulés : aucune suppression de fichiers réelle
  ni aucun contact avec un tracker privé n'est effectué dans la suite de tests.

---

# JOAL Smart Controller v1.6 — Inventaire et compteurs corrigés


## Correction v1.6 : inventaire fiable des torrents tagués

La version 1.5 comptait bien tous les torrents renvoyés par l’API qBittorrent,
mais ne rafraîchissait ce compteur que lors de la **surveillance des trackers**
(`refreshSeconds`, 180 secondes par défaut). Le contrôle rapide ne s’exécutait
que lorsqu’un processus JOAL tournait. Cela pouvait masquer pendant plusieurs
minutes l’ajout d’un tag ou la reprise d’un torrent.

La v1.6 sépare désormais deux fréquences :

- **Inventaire qBittorrent** : toutes les `guardSeconds` (5 secondes par défaut),
  même en `dryRun`, sans processus JOAL. Toutes les entrées portant le tag sont
  affichées quel que soit leur état, y compris `uploading`, `stoppedUP`,
  `downloading` ou `stalledUP`.
- **Évaluation des règles et des trackers** : toutes les `refreshSeconds` ou
  immédiatement à la prochaine itération lorsqu’un changement d’inventaire ou
  d’état est détecté. Les nombres de leechers sont ceux du dernier contrôle.
- **WebUI** : relit l’état du serveur toutes les 5 secondes. Le dashboard affiche
  « Torrents tagués », « Sélections », « Éligibles » et
  « Bloqués / à vérifier », ainsi que les deux horodatages séparés.
- **Une ligne par torrent** : lorsqu’un torrent correspond à plusieurs règles,
  il n’est plus compté plusieurs fois dans la liste ni dans les indicateurs.
  Même sans règle active, un torrent tagué apparaît dans le tableau.
- **Connexion qBittorrent indisponible** : le nombre de torrents tagués est
  indiqué par `—` (inconnu), et **pas zéro**, pour éviter une interprétation erronée.

Exemple avec **un seul torrent tagué** :

| État qBittorrent | Tagués | Éligibles | Sélections | Bloqués |
|---|---:|---:|---:|---:|
| Téléchargement en cours | 1 | 0 | 0 | 1 |
| Complet, arrêté (règles satisfaites) | 1 | 1 | 1 | 0 |
| Partage relancé | 1 | 0 | 0 | 1 |
| Tag retiré | 0 | 0 | 0 | 0 |

Les changements deviennent visibles après les interrogations successives de
l’API qBittorrent et de la WebUI, pas nécessairement instantanément. Si
qBittorrent est inaccessible, les valeurs ne sont pas connues.

**Important** : la v1.6 ne modifie pas la logique d’annonces JOAL, les
règles Tracker + tag, la sélection sécurisée des torrents arrêtés, le ratio
local, les variables d’environnement ni le modèle Docker Unraid.

Tests : **41 tests Python réussis**, syntaxe JavaScript, références d’éléments
HTML et template XML validés. Construction Docker et exécution sur Unraid
réels non vérifiées dans l’environnement de génération.

## Nouveauté v1.5

- Nouvel onglet **Importer un torrent** : sélectionner un fichier `.torrent`, cliquer sur **Analyser** pour afficher son nom et ses domaines de trackers, puis **Importer dans qBittorrent**.
- Le contrôleur vérifie le format bencode (maximum 50 Mio), la présence d'un tracker HTTP(S) correspondant à une **règle active** et l'absence d'un torrent ayant le même infohash dans qBittorrent.
- Importation directement par l'API Web qBittorrent, avec le tag configuré dans `qbitTag` (par défaut `joal`) et l'option d'ajout à l'**arrêt** (`paused=true`). L'import ne lance pas le torrent.
- Le chemin de destination est facultatif : laisser vide pour le chemin de téléchargement par défaut de qBittorrent. S'il est fourni, il doit être un chemin **visible depuis qBittorrent**, pas depuis le conteneur Smart Controller.
- Le contrôleur n'enregistre pas le fichier `.torrent` importé sur disque : il le valide puis le transmet à qBittorrent. Les URL d'annonce et passkeys restent absentes des réponses de prévisualisation.
- La politique de sélection **Tracker + tag** est conservée. Un torrent nouvellement importé reste inéligible tant que qBittorrent ne le considère pas comme **100 % complet et arrêté**. Si les données existent déjà, vérifier leur emplacement et lancer un contrôle d'intégrité dans qBittorrent si nécessaire.
- Un résultat « importation acceptée » confirme la réception de la requête par qBittorrent ; il ne garantit pas que les fichiers soient présents, que le torrent soit complet, ni que des transferts réels aient eu lieu.

## Installer la mise à jour sur Unraid (template natif)

1. Sauvegarder `data/` et `data/policy.json`.
2. Extraire le contenu de l'archive par-dessus `/mnt/user/appdata/joal-smart-controller/` **sans supprimer `data/` et `.env`**.
3. Reconstruire l'image :
   ```bash
   cd /mnt/user/appdata/joal-smart-controller
   docker build -t joal-smart-controller:local .
   ```
4. Dans Unraid : **Docker → joal-smart-controller → Edit → Apply**.
5. Recharger la WebUI (`Ctrl+F5`) et ouvrir **Importer un torrent**.

Si le conteneur est géré par Compose plutôt que par le template natif : `docker compose up -d --build` (ne pas utiliser les deux méthodes simultanément).

## Vérifications

Les tests sont dans `tests/` : `python3 -m unittest discover -s tests -v`.
**Validation initiale de la v1.5 : 34 tests réussis**, dont 9 nouveaux tests v1.5 couvrant le parsing multipart, la confidentialité des passkeys, les doublons, les trackers refusés, l’authentification WebUI et l’envoi multipart à qBittorrent. Syntaxes Python/JavaScript et modèle XML Unraid vérifiés. **La construction Docker, la liaison avec qBittorrent 5.3.0 sur un NAS réel, et JOAL lui-même n’ont pas été exécutés dans cet environnement.**

## Port TCP 49152

La v1.5 **n’ouvre ni ne publie le port 49152** dans son template Unraid ou Docker Compose. L’importation des fichiers `.torrent` est entièrement gérée par l’API Web de qBittorrent ; elle n’ouvre pas de connexion BitTorrent avec des pairs. Le fait qu’un port soit ouvert ne démontre pas qu’un fichier est réellement partagé, et le contrôleur ne renvoie pas de fausses réponses BitTorrent pour simuler la présence de données.


---

# JOAL Smart Controller v1.4 — garde qBittorrent et ratio local

## Nouveautés

- JOAL ne retient un torrent que lorsque qBittorrent le signale **complet et arrêté** (`stoppedUP`, compatibilité `pausedUP`). Un état `uploading`, `stalledUP`, `queuedUP` ou inconnu est exclu.
- Un garde-fou distinct vérifie les états toutes les **5 secondes** par défaut (`guardSeconds`, réglable de 3 à 60). Il arrête le **processus JOAL de toute la règle** si un torrent en cours redémarre, disparaît ou si l'API échoue. **Ce n'est pas atomique** : une courte période de chevauchement reste théoriquement possible avant la détection.
- `targetRatio` (0 = désactivé) par règle, **basé uniquement sur le champ `ratio` mesuré par qBittorrent**. Si le ratio local atteint la cible et que le torrent est complet et arrêté, ajouter d'abord `JoalCompleted` (paramétrable par `completedTag`), puis supprimer le tag `joal`.
- `dryRun: true` ne modifie aucun tag et ne lance aucun JOAL.
- Si un torrent annonce plusieurs trackers correspondant à plusieurs règles actives, **une seule règle peut le prendre en charge** (la première règle admissible dans l'ordre de `rules`). Les autres lignes indiquent « Déjà affecté » ; une duplication de session JOAL est évitée par construction.
- Le tableau de bord affiche désormais une **suspension** après déclenchement du garde, sans attendre le prochain cycle principal de surveillance.
- Si un processus Java refuse de s'arrêter, son handle est conservé et une alerte est affichée ; il ne sera pas considéré comme arrêté par défaut.
- Les ratios annoncés par JOAL ne sont PAS inclus dans le ratio qBittorrent ; le contrôleur ne prétend pas connaître le ratio accepté par un tracker. Le ratio ne progresse normalement pas pendant que le torrent est arrêté dans qBittorrent. **Un ratio cible de 1.20 dans cette version n'arrêtera donc pas automatiquement une activité JOAL lorsqu'un tracker privé affichera 1.20 : ce n'est pas la même mesure.** Pour un seuil fondé sur le ratio *du tracker*, il faut une API officielle du tracker. JOAL `uploadRatioTarget` mesure une session, pas le ratio global du tracker.
- Toutes les anciennes configurations v1.3 restent valides : `targetRatio: 0`, `completedTag: JoalCompleted`, `guardSeconds: 5` par défaut.
- Le moteur JOAL upstream reste inchangé. Ce projet est un compagnon, et non un fork Java.

## Mise à jour Unraid

1. **Sauvegarder** `data/policy.json` et tout le dossier `data/`.
2. Recopier les fichiers de l'archive par-dessus le dossier du projet (sans effacer `.env` ni `data/`).
3. Dans le terminal Unraid :

   ```bash
   cd /mnt/user/appdata/joal-smart-controller
   docker build -t joal-smart-controller:local .
   ```

4. Docker → `joal-smart-controller` → **Edit** → **Apply**.
5. Recharger la WebUI (`Ctrl+F5`). Si `dryRun` est activé, vérifier les motifs de sélection avant les modifications effectives.

## Précautions

- La détection ne garantit pas une exclusion simultanée absolue, faute d'opération atomique entre deux processus BitTorrent. La suspension ne peut intervenir qu'après un contrôle et un délai de terminaison ; le contrôleur **n'est pas une méthode d'invisibilité** vis-à-vis des trackers.
- Le réamorçage d'un processus JOAL reste soumis à la temporisation anti-relance de 15 minutes. L'option `guardSeconds` est un intervalle de vérification et ne garantit pas un délai maximum d'arrêt en cas de lenteur réseau/API.
- La mise à jour des tags est faite uniquement en mode actif, et uniquement après un ratio local confirmé; ajouter le tag avant de retirer l'ancien limite le risque de perte en cas de panne.
- Les annonces fictives ne constituent pas des transferts réels ; un tracker peut les ignorer.

---

# Historique v1.3

## Nouveautés v1.3

- Interface sombre, responsive, inspirée du principe de tableau de bord de JOAL, sans dépendance CDN.
- **Vue d’ensemble** : connexion qBittorrent, mode actuel, torrents tagués, éligibles et retenus, état des règles.
- **Torrents suivis** : recherche et filtres par règle/statut ; domaine du tracker, leechers, progression, état qBittorrent et motif exact (seuil, limite, torrent incomplet, tracker inactif, etc.).
- **Règles & trackers** : ajout, duplication, suppression et modification de chaque règle depuis des champs graphiques, avec profils `.client` suggérés.
- **Paramètres** : `dryRun`, tag qBittorrent, fréquence de rafraîchissement et maximum de torrents, sous forme de champs et interrupteur.
- **JSON avancé** conservé pour les utilisateurs souhaitant modifier `policy.json` manuellement.
- La vue des torrents **ne contient jamais les passkeys des trackers** : seuls les domaines, et non les URL `/announce`, y apparaissent.
- La présélection est séparée de l'éligibilité : être éligible ne signifie pas forcément être retenu (limite par règle).
- Les torrents incomplets portant le tag sont visibles pour diagnostic, **mais ne sont jamais retenus**.
- Authentification qBittorrent facultative si le réseau Docker est autorisé sans identifiants (amélioration v1.2 conservée).
- Durcissement de l'API de configuration : contrôle de l'origine HTTP des modifications, réponse propre en cas de taille invalide, en-têtes de sécurité.

**Important :** les valeurs min/max affichées sont les vitesses *paramétrées* pour JOAL, pas des vitesses de transfert mesurées. En mode `dryRun: true`, aucune annonce JOAL n'est envoyée, et les torrents « retenus » constituent seulement une prévisualisation. Les statuts JOAL indiquent un processus lancé, pas une preuve de transfert réel ou d'acceptation par un tracker.

## Mettre à jour depuis v1.2 avec le template Unraid

1. Sauvegarder le fichier `/mnt/user/appdata/joal-smart-controller/data/policy.json` et conserver les données du dossier `data/`.
2. Décompresser **le contenu** du dossier `joal-smart-controller/` de cette archive **par-dessus** `/mnt/user/appdata/joal-smart-controller/`. Ne pas créer de deuxième dossier `joal-smart-controller` imbriqué. Ne pas supprimer `.env` ou `data/`.
3. Sur le terminal Unraid :

   ```sh
   cd /mnt/user/appdata/joal-smart-controller
   docker build -t joal-smart-controller:local .
   ```

4. Dans **Docker → joal-smart-controller → Edit → Apply**, recréer le conteneur à partir de l'image locale, sans modifier les chemins de volumes/réseaux existants.
5. Ouvrir **WebUI** puis forcer le rechargement du navigateur (Ctrl+F5 si nécessaire). Les règles existantes sont reprises automatiquement depuis `data/policy.json`.

Si vous utilisez encore **Docker Compose**, lancer à la place `docker compose up -d --build`. Ne pas lancer Compose après avoir migré vers le template Unraid.

Les réglages `Network Type`, les variables qBittorrent, les ports et les volumes restent gérés par **Edit** dans Unraid. Le fichier `.env` n'est pas lu par un conteneur créé directement depuis un template Unraid.

## Vérifications effectuées

- 15 tests unitaires / d'intégration HTTP réussis : API, sécurité de modification, sauvegarde JSON, compatibilité de l'authentification facultative, règles et métadonnées.
- Analyse syntaxique JavaScript et Python réussie.
- Format XML du template Unraid vérifié.
- **Non vérifié sur un conteneur Unraid réel** dans cet environnement. Un contrôle navigateur automatisé a été tenté mais bloqué par la restriction de navigation locale de l'environnement de test ; il ne faut donc pas considérer l'interface comme validée de bout en bout dans Chromium.
- Pas de validation contre des trackers réels. L'acceptation des annonces et le comportement du JOAL amont restent indépendants de ce tableau de bord.

---

## Correction v1.2 — authentification qBittorrent

Le contrôleur vérifie désormais l'accès sans authentification via `/api/v2/app/version` avant d'essayer les identifiants. Cela prend en charge l'option qBittorrent **Ignorer l’authentification pour les clients des sous-réseaux autorisés**, à condition que l'IP Docker du contrôleur soit effectivement autorisée. Si l'API renvoie 401 ou 403, la connexion classique avec identifiant et mot de passe est tentée. Un 403 peut aussi être lié à un bannissement temporaire.

Pour une installation créée depuis le template Unraid : remplacer les fichiers du projet, sans effacer `.env` ni `data/`, exécuter `docker build -t joal-smart-controller:local .` dans le répertoire du projet, puis ouvrir **Docker > Edit > Apply** afin de recréer le conteneur avec l'image mise à jour. Un simple redémarrage du conteneur ne recharge pas la nouvelle image.

---

# JOAL Smart Controller — Unraid / qBittorrent 5.3.0

**Prototype technique** de contrôleur de politiques pour JOAL, distribué avec son Docker Compose.

> **Ce n'est pas un fork natif de JOAL** : le code Java upstream reste inchangé. Le contrôleur
> lance un JOAL distinct pour chaque règle active et gère ses fichiers `.torrent`.
> Il ne modifie ni les compteurs internes de qBittorrent ni ses données enregistrées.
> Aucune compatibilité avec un tracker réel n'a été vérifiée.

## Fonctionnalités

- Règles distinctes pour les domaines de trackers (`trackerDomains`).
- Seuil minimal de **leechers du tracker** (`minLeechers`) ; les peers connectés ne sont pas utilisés.
- Vitesses JOAL min/max **par règle** (`minUploadRate`, `maxUploadRate`, en kB/s selon JOAL).
- Limite de torrents par règle (`simultaneousSeed`).
- Surveillance des torrents portant un tag qBittorrent dédié (par défaut `joal`) ; seuls les torrents complets peuvent être retenus.
- Seulement les trackers HTTP(S) déclarés actifs par qBittorrent (`status = 2`) et
  ayant au moins le nombre exigé de leechers peuvent être sélectionnés.
- Filtrage de l'annonce des torrents exportés tout en préservant exactement les octets
  originaux du dictionnaire `info` / infohash.
- Tableau de bord Web avec éditeur graphique des règles et onglet JSON avancé, protégé par mot de passe.
- Arrêt de JOAL en cas d'erreur de connexion à qBittorrent (mode échec fermé).
- Mode de simulation locale **`dryRun: true` activé par défaut** : aucune annonce JOAL.

## Pré-requis

- Unraid avec Docker Compose et sortie Internet pour la construction de l'image.
- qBittorrent 5.3.0, WebUI API activée et accessible depuis le conteneur.
- Java : inclus dans l'image construite.
- Un fichier client `.client` JOAL adapté, fourni dans le répertoire `clients`
  de la distribution JOAL ; ne pas confondre avec les plugins de recherche de qBittorrent.

## Installation

1. Décompresser le projet sur Unraid, par exemple `/mnt/user/appdata/joal-smart-controller/`.
2. Copier `.env.example` en `.env` et configurer `ADMIN_PASSWORD`. La connexion qBittorrent peut être saisie dans Paramètres → Connexion qBittorrent ou via les variables Docker. Redémarrer le conteneur après avoir enregistré la connexion dans la Web UI.
   `QBIT_USERNAME` et `QBIT_PASSWORD` sont facultatifs si qBittorrent autorise explicitement le réseau Docker sans authentification. Choisir un mot de passe administrateur de 12 caractères minimum. Ne jamais rendre `.env` public.
3. Créer le dossier `data/clients/`, puis y mettre les profils `.client` extraits de la
   distribution officielle JOAL : https://github.com/anthonyraymond/joal/releases
4. Lancer à partir du dossier du projet :

   ```sh
   docker compose up -d --build
   ```

5. Ouvrir `http://IP_UNRAID:8788/` (authentification Basic, utilisateur `admin`, mot de passe
   défini dans `.env`). Utiliser uniquement en réseau privé ou derrière un reverse proxy HTTPS.
6. Dans qBittorrent, appliquer le tag `joal` aux torrents complets que l'on veut surveiller.
7. Éditer les règles via le tableau de bord : sélectionner le nom réel du fichier `.client`,
   saisir les domaines réels et activer la règle avec `"enabled": true`.
8. **Conserver `dryRun: true` pour valider la sélection.** Dans ce mode, le contrôleur
   ne transmet pas d'annonce de volume fictif ; les vitesses sont des paramètres descriptifs.

## Exemple de règle

```json
{
  "dryRun": true,
  "qbitTag": "joal",
  "refreshSeconds": 180,
  "maxCandidates": 250,
  "rules": [
    {
      "id": "tracker-exemple",
      "enabled": true,
      "trackerDomains": ["tracker.exemple.org"],
      "minLeechers": 3,
      "minUploadRate": 30,
      "maxUploadRate": 160,
      "simultaneousSeed": 10,
      "client": "qbittorrent-5.3.0.client"
    }
  ]
}
```

`trackerDomains` contient des noms de domaine, **pas** des URL de trackers complètes.
Les URL complètes peuvent contenir des clés d'accès personnelles. Le contrôleur n'affiche
pas ces clés dans son interface.

## Fonctionnement et limites

- JOAL ne possède pas de logique native de règles par tracker : un processus Java par
  règle active est donc nécessaire. Cette architecture consomme davantage de RAM.
- Pour empêcher les modifications de sélection trop fréquentes, le redémarrage d'un
  même processus JOAL est limité à un intervalle de 15 minutes. **Si un torrent cesse
  d'être admissible, le contrôleur arrête immédiatement le processus**, puis attend
  avant de le redémarrer. Cela peut entraîner une interruption volontaire.
- Un tracker UDP n'est pas pris en charge ; les compteurs inconnus (`-1`) ou les trackers
  non fonctionnels ne sont jamais considérés comme admissibles.
- Les torrents non complets ou dépourvus du tag sélectionné sont ignorés.
- Un nom de domaine doit correspondre exactement ou comme sous-domaine. Les règles
  actives ne peuvent pas cibler des domaines qui se recouvrent.
- Si `maxCandidates` est dépassé, la surveillance s'arrête en mode échec fermé.
- L'information « nombre de leechers » de l'API dépend du dernier retour de chaque
  tracker à qBittorrent ; elle n'est pas du temps réel.
- Le démarrage de JOAL et l'acceptation de ses annonces par des trackers externes
  n'ont pas été validés en conditions réelles. Cela peut contrevenir aux conditions
  des trackers et exposer le compte à des sanctions.
- Le mot de passe de l'interface est transmis par Basic Auth : n'exposez pas
  le port sur Internet en HTTP.
- Seule la configuration du contrôleur est modifiée : le contenu physique des torrents
  reste dans qBittorrent, et aucun fichier des téléchargements ne sera modifié.

## Tests locaux

```sh
python3 -m unittest discover -s tests -v
```

Tests des règles, des correspondances de domaines, des seuils de leechers,
ainsi que de la préservation de l'infohash en filtrant le fichier torrent.

## Source et licences

- JOAL : https://github.com/anthonyraymond/joal (Apache 2.0)
- qBittorrent Web API : https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0)

Le dépôt Java JOAL n'est pas inclus dans cette archive. L'image Docker le récupère
comme dépendance de construction. Ne pas publier l'archive `.env` ni les fichiers `.torrent`
privés, puisqu'ils peuvent contenir des clés de tracker.

## Réseau Docker et intégration à l'interface Unraid (v1.1)

Deux méthodes possibles, **à ne pas utiliser en même temps**.

### Méthode A : rester avec Docker Compose

Dans le fichier `.env` existant, ajouter si nécessaire :

```env
DOCKER_NETWORK=bridge
WEBUI_PORT=8788
```

- `DOCKER_NETWORK` : choisir un réseau déjà existant. Vérifier avec `docker network ls`.
  Exemples : `bridge`, `br0`, `mon_reseau`.
- `WEBUI_PORT` : port sur l'hôte (ex. 8788). Le port interne est fixé à 8788.
- Ne pas choisir `host` dans la méthode Compose : `ports` et `network_mode: host` sont incompatibles.
- Le label `net.unraid.docker.webui` fait apparaître **WebUI** dans le menu Docker Unraid.
- **Edit** ne sera pas automatiquement disponible : le gestionnaire Docker Unraid
  ne dispose pas de template natif pour un conteneur créé depuis Compose.

Dans le terminal Unraid :

```sh
cd /mnt/user/appdata/joal-smart-controller
docker compose config
docker compose up -d --build
```

Le fichier `.env` existant n'est pas remplacé par l'archive de mise à jour.

### Méthode B : disposer de « Edit » et « WebUI » natifs

Le template fourni se trouve dans `unraid/my-joal-smart-controller.xml`.
Cette méthode permet de choisir le réseau dans le menu **Network Type** et de modifier
les variables, les ports et les volumes via **Docker > icône > Edit**.

**Migration depuis Compose (préserve `/data`) :**

1. Conserver le répertoire `/mnt/user/appdata/joal-smart-controller/data`.
   Il stocke les règles et l'état. Sauvegarder également votre `.env`.
2. Construire une image locale portant le nom utilisé par le template, puis arrêter
   et retirer **uniquement** le conteneur Compose :

   ```sh
   cd /mnt/user/appdata/joal-smart-controller
   docker build -t joal-smart-controller:local .
   docker compose down
   mkdir -p /boot/config/plugins/dockerMan/templates-user
   cp unraid/my-joal-smart-controller.xml /boot/config/plugins/dockerMan/templates-user/my-joal-smart-controller.xml
   ```

   `docker compose down` ne supprime ni `./data` ni votre fichier `.env`.
   Ne pas utiliser `docker compose down -v`.
3. Dans l'interface Unraid, ouvrir **Docker > Add Container** ; dans le menu
   **Template**, sélectionner **joal-smart-controller** parmi les modèles utilisateur.
4. Choisir **Network Type** (bridge, br0, autre réseau disponible).
5. Renseigner les mêmes valeurs `QBIT_URL`, `QBIT_USERNAME`, `QBIT_PASSWORD`,
   `ADMIN_PASSWORD` que dans l'ancien `.env`. Le template ne lit pas `.env`.
   Le port interne doit rester à **8788** ; le port hôte peut changer.
6. Vérifier que le chemin `/data` reste
   `/mnt/user/appdata/joal-smart-controller/data`, puis cliquer sur **Apply**.
7. **Docker > icône joal-smart-controller** fournit normalement les actions
   **Edit** (variables, réseau, ports, volumes) et **WebUI** (tableau de bord).

**Ne pas relancer `docker compose up` après la migration :** cela tenterait de
créer un second conteneur portant le même nom. Pour mettre à jour le code, reconstruire
l'image locale puis recréer le conteneur via le template Unraid.

**Accès réseau :** si vous sélectionnez `br0`/macvlan/ipvlan, le conteneur
peut posséder sa propre adresse IP et les règles d'accès depuis l'hôte Unraid
peuvent différer du mode `bridge`. Il faut vérifier la connexion qBittorrent
et l'URL du bouton WebUI dans ce cas. Ne pas exposer cette interface HTTP
à Internet ; utiliser un accès réseau privé ou un reverse proxy HTTPS.


## v1.11.6 — Partage réel et état JOAL

L'onglet **Torrents suivis** comporte une colonne **Envoi réel / progression**.
Elle affiche le compteur `uploaded` (repli `total_uploaded`) de l'API qBittorrent,
le volume cible calculé à partir de la taille du torrent et du ratio local défini
dans la règle, et une barre en pourcentage. Elle affiche aussi depuis combien
de temps le processus JOAL de ce torrent est actif.

**Limite volontaire :** le moteur Java JOAL n'est pas instrumenté dans ce
contrôleur pour exposer ses octets simulés ; la barre ne représente ni ces
annonces fictives ni un volume accepté par le tracker. Le ratio affiché est
uniquement celui de qBittorrent. Lorsque le ratio cible n'est pas défini ou
que les compteurs sont absents, aucune progression inventée n'est affichée.

Les largeurs de colonnes sont conservées lors de la migration depuis la v1.11.5.


## v1.16 — Importation simplifiée

- L'importation WebUI reste **un fichier `.torrent` à la fois** (aucun import multiple via le bouton « Analyser » dans les anciennes versions).
- Dès la sélection du fichier, **l'analyse des métadonnées se lance automatiquement** ; le bouton « Analyser » a été supprimé.
- « Ajouter à JOAL uniquement » enregistre le fichier directement, **sans seconde fenêtre de confirmation**, après une analyse réussie.
- Les contrôles existants restent actifs : extension et taille du fichier, conformité des métadonnées, doublons et règles de tracker.
- Un fichier invalide bloque l'ajout. Après une erreur, le même fichier peut être sélectionné de nouveau pour réessayer.
- Si le choix du fichier change pendant l'analyse, une réponse retardée ne peut pas valider un autre fichier.
- Les fichiers restent dans `data/torrents/` ; aucun fichier n'est envoyé à qBittorrent.
