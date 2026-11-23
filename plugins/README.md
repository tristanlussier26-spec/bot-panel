# Catalogue de plugins Bot Panel

Les plugins qu'on installe depuis l'onglet **Plugins** d'un bot (fait avec le modèle **Bot NG**). Le panel lit `index.json` ; chaque entrée pointe vers un zip de `dist/` avec son sha256.

| Plugin | Ce qu'il fait | Intents à activer |
|---|---|---|
| [`nationsglory`](nationsglory/) | Grades d'un pays NationsGlory ↔ rôles Discord, `/lier` avec validation, annonces du pays, salon de stats. Alertes de guerre, de power et de diplomatie, pays surveillés, rapport du jour, warzone du jour et de la semaine, annonces officielles du forum, marché des céréales, objets de l'hôtel des ventes, loteries, rôles « en jeu » et « inactif », salon des membres en jeu avec leurs têtes, un salon et un rôle par entreprise du pays (patron, employés). Tout se règle dans Options du bot : format du surnom, rôle à mentionner, salons séparés, fréquence du rapport, salons privés, salons et rôle permis pour les commandes, réponses privées. Commandes : `/pays`, `/guerres`, `/guerre`, `/classement-pays`, `/historique`, `/comparer`, `/enligne`, `/inactifs`, `/alliance`, `/joueur`, `/warzone`, `/competences`, `/marche`, `/hdv`, `/loteries`, `/entreprises`, `/entreprise`, `/membre`, `/membres`, `/classement-membres`, `/employe`, `/statut-ng`, `/rapport`. Une seule clé de l'API officielle (variable `NG_API_KEY`) pour tous les serveurs. | Server Members |
| [`bienvenue`](bienvenue/) | Bienvenue et départ, MP de bienvenue, rôle d'arrivée retiré par un bouton ou par la liaison NationsGlory. | Server Members |
| [`tickets`](tickets/) | Panneau de tickets, salon privé avec le staff, transcription à la fermeture. | Server Members |
| [`niveaux`](niveaux/) | XP en écrivant, rôles en récompense, `/rang`, `/classement`. | — |
| [`moderation`](moderation/) | `/warn`, `/timeout`, `/kick`, `/ban`, `/clear`… avec les avertissements gardés. | — |
| [`logs`](logs/) | Salon de logs : messages, membres, rôles, vocal, et ce que les autres plugins envoient. | Server Members, Message Content |
| [`automod`](automod/) | Anti-spam, anti-invitations, anti-liens, mots interdits, mentions en masse, anti-raid. | Server Members, Message Content |
| [`adminbuild`](adminbuild/) | `/adminbuild` crée les rôles et salons d'un serveur de pays, puis règle les autres plugins. | — |
| [`compteurs`](compteurs/) | Salons vocaux qui se renomment tout seuls : membres, humains, bots, boosts, membres du pays NG et ceux en ligne. `/compteurs-creer`. | Server Members |
| [`recrutement`](recrutement/) | `/postuler` ou bouton « Postuler » : questions à toi, Accepter/Refuser par le staff, rôles donnés, réponse en MP. | Server Members |
| [`roles`](roles/) | Jusqu'à 3 panneaux de rôles à boutons (alertes, jeux, couleurs), mode « un seul à la fois ». | — |
| [`evenements`](evenements/) | Guerres, réunions et événements : présences par boutons, rappels automatiques avec mention. | — |
| [`musique`](musique/) | Lecteur vocal : liens audio, radios en ligne, fichiers ; file, pause, volume, rôle DJ. Pas YouTube ni Spotify. Installe ffmpeg (~200 Mo) au premier démarrage. | — |

Pour écrire un plugin ou en ajouter un ici : voir « Plugins » dans le [README de Bot Panel](../README.md#plugins). Après une modification, monte la `version` dans `plugin.json` et lance `python scripts/build_catalog.py`.
