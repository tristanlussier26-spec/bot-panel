# Catalogue de plugins Bot Panel

Les plugins qu'on installe depuis l'onglet **Plugins** d'un bot. Le panel lit `index.json` ; chaque entrée pointe vers un zip de `dist/` avec son sha256.

| Plugin | Ce qu'il fait |
|---|---|
| [`nationsglory`](nationsglory/) | Grades d'un pays NationsGlory ↔ rôles Discord, `/lier` avec validation, annonces du pays, salon de stats. Une seule clé de l'API officielle (variable `NG_API_KEY`) pour tous les serveurs qui utilisent le bot. |

Pour écrire un plugin ou en ajouter un ici : voir « Plugins » dans le [README de Bot Panel](../README.md#plugins). Après une modification, lance `python scripts/build_catalog.py`.
