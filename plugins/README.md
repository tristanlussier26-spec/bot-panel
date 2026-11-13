# Catalogue de plugins Bot Panel

Les plugins qu'on installe depuis l'onglet **Plugins** d'un bot (fait avec le modèle **Bot NG**). Le panel lit `index.json` ; chaque entrée pointe vers un zip de `dist/` avec son sha256.

| Plugin | Ce qu'il fait | Intents à activer |
|---|---|---|
| [`nationsglory`](nationsglory/) | Grades d'un pays NationsGlory ↔ rôles Discord, `/lier` avec validation, annonces du pays, salon de stats. Une seule clé de l'API officielle (variable `NG_API_KEY`) pour tous les serveurs. | Server Members |
| [`bienvenue`](bienvenue/) | Bienvenue et départ, MP de bienvenue, rôle d'arrivée retiré par un bouton ou par la liaison NationsGlory. | Server Members |
| [`tickets`](tickets/) | Panneau de tickets, salon privé avec le staff, transcription à la fermeture. | Server Members |
| [`niveaux`](niveaux/) | XP en écrivant, rôles en récompense, `/rang`, `/classement`. | — |
| [`moderation`](moderation/) | `/warn`, `/timeout`, `/kick`, `/ban`, `/clear`… avec les avertissements gardés. | — |
| [`logs`](logs/) | Salon de logs : messages, membres, rôles, vocal, et ce que les autres plugins envoient. | Server Members, Message Content |
| [`automod`](automod/) | Anti-spam, anti-invitations, anti-liens, mots interdits, mentions en masse, anti-raid. | Server Members, Message Content |
| [`adminbuild`](adminbuild/) | `/adminbuild` crée les rôles et salons d'un serveur de pays, puis règle les autres plugins. | — |

Pour écrire un plugin ou en ajouter un ici : voir « Plugins » dans le [README de Bot Panel](../README.md#plugins). Après une modification, monte la `version` dans `plugin.json` et lance `python scripts/build_catalog.py`.
