# Bot Panel

Un panel web pour héberger des bots Discord, un peu comme Pterodactyl, mais fait juste pour ça. Tu peux aussi y monter des serveurs Discord à partir d'un modèle.

- **Héberger n'importe quel bot** en Python, en Node.js ou avec une image Docker de ton choix. Chaque bot roule dans son propre conteneur, avec ses limites de RAM et de CPU.
- **Console en direct** : tu vois ce que ton bot écrit et tu peux lui envoyer des commandes. Tu as aussi les boutons démarrer, redémarrer, arrêter et tuer, et la RAM et le CPU s'affichent en temps réel.
- **Fichiers** : éditeur dans le navigateur, téléversement par glisser-déposer, décompression de .zip et téléchargement.
- **Déploiement depuis GitHub** : clone ton dépôt et mets-le à jour en un clic. Les dépôts privés marchent avec un jeton.
- **Variables d'environnement** : ton `DISCORD_TOKEN` est caché et jamais réaffiché.
- **Redémarrage automatique** si le bot plante. S'il plante en boucle, le panel arrête d'essayer et te le dit dans la console.
- **Constructeur de serveurs Discord** : tu décris les rôles, catégories, salons et permissions dans un modèle, tu vois le plan, puis tu l'appliques avec un de tes bots. Tu peux aussi copier un serveur existant ou importer un modèle discord.new.
- **Modèle « Bot NG »** : un bot Discord pour un pays NationsGlory, prêt à rouler. Tu mets son jeton, et tu choisis ce qu'il fait avec des plugins.
- **Plugins** : des modules à ajouter à un bot en un clic, depuis le catalogue GitHub ou un .zip : synchro des grades NationsGlory, bienvenue, tickets, niveaux, modération, logs, auto-modération, construction du serveur, salons compteurs, recrutement, rôles à boutons, événements du pays, musique.
- **Plusieurs comptes** : connexion avec Discord. L'admin approuve ses amis et fixe leurs limites (nombre de bots, RAM, disque). Chacun voit seulement ses bots.

## Sécurité : à lire avant d'installer

Le panel pilote Docker (`/var/run/docker.sock`). Il a donc les pleins pouvoirs sur la machine où il roule. **Installe-le dans une VM juste pour lui**, jamais directement sur l'hôte Proxmox.

Chaque bot roule :

- en utilisateur non-root (1000) ;
- sans capacités Linux, avec `no-new-privileges` ;
- avec un système de fichiers en lecture seule, sauf son dossier et `/tmp` ;
- avec des limites de RAM, de CPU et de processus.

C'est solide, mais un conteneur reste moins isolé qu'une VM. Donne accès seulement à des gens de confiance.

Le script `scripts/isoler-reseau.sh` empêche les bots d'atteindre ton réseau local (routeur, Proxmox, tes autres VM) et la machine du panel. Ils gardent l'accès à Internet. Lance-le (voir l'installation, étape 6).

## Installation

Sur une VM Debian 12 ou Ubuntu avec 2 Go de RAM ou plus (selon tes bots).

**1. Installer Docker** (passe cette étape si Docker est déjà là) :

```sh
curl -fsSL https://get.docker.com | sudo sh
```

**2. Télécharger le panel** :

```sh
sudo git clone https://github.com/tristanlussier26-spec/bot-panel.git /opt/bot-panel
cd /opt/bot-panel
sudo cp .env.example .env
```

**3. Créer l'application Discord pour la connexion** :

1. Va sur <https://discord.com/developers/applications> et clique « New Application ».
2. Dans **OAuth2**, copie le **Client ID** et le **Client Secret** (Reset Secret).
3. Dans **Redirects**, ajoute `http://IP-DE-TA-VM:8080/auth/callback`, avec exactement la même adresse que `PUBLIC_URL`.

Cette application sert seulement à se connecter au panel. Tes bots ont chacun leur propre application et leur propre jeton.

**4. Remplir `.env`** (`sudo nano .env`) :

- `PUBLIC_URL` : `http://IP-DE-TA-VM:8080`
- `DISCORD_CLIENT_ID` et `DISCORD_CLIENT_SECRET` : ceux de l'étape 3
- `ADMIN_IDS` : ton ID Discord. Active le mode développeur dans Discord, puis fais clic droit sur ton profil › Copier l'identifiant.

**5. Démarrer** :

```sh
sudo docker compose up -d --build
sudo docker compose logs -f     # Ctrl+C pour sortir
```

Ouvre `http://IP-DE-TA-VM:8080` et connecte-toi avec Discord.

**6. Isoler les bots du réseau local** (recommandé) :

```sh
sudo sh scripts/isoler-reseau.sh
# Pour que ça revienne après chaque redémarrage de la machine :
sudo cp scripts/botpanel-isolation.service /etc/systemd/system/
sudo systemctl enable botpanel-isolation
```

### Mettre à jour

```sh
cd /opt/bot-panel
sudo git pull
sudo docker compose up -d --build
```

Les bots continuent de rouler pendant la mise à jour du panel. Leurs fichiers, la base de données et les sessions sont dans `data/`.

## Utilisation

### Ton premier bot

1. **Nouveau bot**, puis choisis Python ou Node.js. Le panel met un petit bot d'exemple dedans.
2. Onglet **Variables** : ajoute `DISCORD_TOKEN` avec le jeton de ton bot (portail développeur › ton application › Bot › Reset Token).
3. **Démarrer**. La console affiche « Connecté en tant que… ».

Pour mettre ton propre code, tu as trois façons :

- glisser tes fichiers dans l'onglet **Fichiers** ;
- téléverser un .zip et le décompresser ;
- mettre l'adresse de ton dépôt GitHub dans **Réglages**.

Au démarrage, le panel installe tout seul les dépendances :

- `requirements.txt` avec pip, pour Python ;
- `package.json` avec npm, pour Node.js.

Si ton bot a son propre site ou panel web, coche **Ouvrir un port web** dans **Réglages**. Le panel lui donne un port de `PORT_RANGE` (25500 à 25599 par défaut) et la variable `SERVER_PORT`, comme Pterodactyl. Le bot doit écouter sur ce port, et son adresse s'affiche à côté de la console.

Avec une **image perso**, tu donnes l'image (ex. `eclipse-temurin:21-jre`) et la commande de démarrage (ex. `java -jar bot.jar`). Seul l'admin peut choisir une image hors de la liste `ALLOWED_IMAGES`.

### Constructeur de serveurs Discord

1. Il te faut un bot avec `DISCORD_TOKEN`, invité sur le serveur avec la permission **Administrateur**. Le lien d'invitation est dans la page.
2. Écris ou modifie le modèle. Tu peux partir de l'exemple, copier un serveur existant ou importer un lien discord.new.
3. **Voir le plan** montre ce qui va être créé, modifié ou supprimé. Rien ne change sur Discord avant que tu cliques **Appliquer**.

Deux modes :

- **Ajouter et modifier** crée ce qui manque et corrige ce qui diffère, sans rien supprimer.
- **Copie exacte** supprime aussi ce qui est pas dans le modèle. Il faut cocher une confirmation.

Le panel touche jamais aux rôles des autres bots ni aux permissions mises membre par membre.

```yaml
name: Mon serveur              # facultatif : renomme le serveur

everyone:
  permissions: [view_channel, send_messages, read_message_history, connect, speak]

roles:                         # du plus haut au plus bas
  - name: Admin
    color: "#e0457b"
    hoist: true                # affiché à part dans la liste des membres
    permissions: [administrator]
  - name: Modo
    permissions: [manage_messages, kick_members, moderate_members]
  - name: Membre

categories:
  - name: Bienvenue
    overwrites:
      "@everyone": {deny: [send_messages]}
    channels:
      - {name: règles, topic: "Lis les règles"}
      - {name: annonces, type: announcement}

  - name: Staff
    private: true              # caché à @everyone
    overwrites:
      Modo: {allow: [view_channel]}
    channels:
      - staff
      - {name: Réunion, type: voice, user_limit: 10}

channels:                      # salons hors catégorie
  - général
```

Voici les règles du modèle :

- **Types de salon** : `text`, `voice`, `announcement`, `stage`, `forum`.
- **Options d'un salon** : `topic`, `slowmode` (en secondes), `nsfw`, `user_limit` (pour un vocal).
- **Permissions d'un salon** : il reprend celles de sa catégorie. Ce que tu mets dans un salon pour un rôle remplace ce que dit la catégorie pour ce rôle. `Rôle: {}` veut dire « rien de spécial pour ce rôle ici ».
- **Noms de permissions** : ce sont ceux de discord.py (`view_channel`, `manage_roles`, etc.). La liste complète est dans la page.

### Bots partagés

Coche **Bot partagé** dans les Réglages d'un bot pour qu'il marche comme DraftBot pour tes amis.

Les comptes approuvés le voient dans **Bots partagés**. Ils peuvent l'inviter sur leur serveur avec un bouton, puis régler ses options. Ils touchent jamais au portail Discord.

Chacun voit seulement les serveurs où il a la permission « Gérer le serveur ». C'est le bot qui donne cette liste dans `state.json`, avec `managers`. Ils voient pas la console, les fichiers ni les variables du bot.

### Options du bot

Un bot peut avoir son propre onglet **Options du bot** dans le panel : des pages de réglages avec des listes de salons et de rôles de chaque serveur. Pas besoin de site web ni de port. Tout passe par des fichiers dans le dossier du bot :

| Fichier | Écrit par | Contenu |
|---|---|---|
| `.botpanel/schema.json` | le bot | les pages de réglages et leurs champs |
| `.botpanel/state.json` | le bot, aux 30 secondes environ | les serveurs (salons, rôles) et les valeurs actuelles |
| `.botpanel/inbox/<id>.json` | le panel | une modification : `{"id", "scope", "page", "values", "user"}` |
| `.botpanel/outbox/<id>.json` | le bot | la réponse : `{"ok": true}` ou `{"ok": false, "error": "..."}` |

Exemple de `schema.json` :

```json
{"title": "Mon bot", "scope": "guild", "pages": [
  {"id": "accueil", "title": "Accueil", "icon": "👋", "description": "Message aux nouveaux.", "fields": [
    {"key": "actif", "label": "Activé", "type": "bool"},
    {"key": "salon", "label": "Salon", "type": "channel"},
    {"key": "message", "label": "Message", "type": "textarea", "help": "{membre} = la mention"}
  ]}
]}
```

Types de champs :

| Type | Ce que ça donne |
|---|---|
| `bool`, `int` (avec `min` et `max`), `text`, `textarea` | une case, un nombre ou du texte |
| `lines` | une liste, une ligne par élément |
| `select` | une liste de choix, dans `choices: [[valeur, texte], …]` |
| `channel`, `category`, `role` | un salon, une catégorie ou un rôle |
| `channels`, `roles` | plusieurs salons ou plusieurs rôles |
| `role_map` | un rôle pour chaque clé de `keys: [[clé, texte], …]` |
| `number_roles` | des paires « nombre › rôle », comme les récompenses de niveau |

Dans `state.json`, chaque serveur a la forme `{"id", "name", "channels": [{"id", "name", "category"}], "categories", "roles", "values": {"page": {"clé": valeur}}, "managers": ["id discord", …]}`. Le fichier a aussi `"app_id"` (l'ID de l'application du bot), qui sert au bouton d'invitation. Les IDs Discord s'écrivent en texte.

Les boutons **Exporter** et **Importer**, sous les pages, copient toutes les options d'un serveur dans un fichier `.json`. Tu peux ensuite les remettre sur un autre serveur ou les restaurer plus tard. À l'import, les salons et les rôles sont retrouvés par leur nom. Ceux qui existent pas sur le serveur sont laissés vides, et le panel les nomme.

Le bot vérifie lui-même ce qu'il reçoit. Le modèle Bot NG en est un exemple complet (`templates/bot-ng/botng/panelbridge.py`).

### Modèles de bots

Dans **Nouveau bot**, choisis un **modèle** : le panel copie un bot prêt à rouler, avec ses plugins de départ. Tu mets son jeton (`DISCORD_TOKEN`) dans Variables, et tu le démarres.

**Bot NG** : un bot Discord pour un pays NationsGlory. Sans plugin, il fait juste le minimum (`/aide`, `/ping`, `/serveur`…) ; tu choisis le reste dans l'onglet Plugins. Il installe d'office la synchro NationsGlory. Un seul Bot NG peut servir plusieurs pays : coche « Bot partagé » et chaque pays le règle dans « Options du bot ».

Quand le modèle a une nouvelle version, l'onglet **Réglages** du bot montre « Mettre à jour » : ça recopie les fichiers du modèle. Les plugins, leurs réglages, les données et les variables restent.

Un modèle, c'est un dossier de `templates/` avec un `template.json` (`id`, `name`, `version`, `description`, `language`, `main_file`, `plugins`). `TEMPLATES_DIR` change le dossier.

### Plugins

L'onglet **Plugins** d'un bot ajoute des modules à ce bot, un peu comme les plugins d'un serveur Minecraft dans Pterodactyl :

- **Catalogue** : la liste vient de `plugins/index.json` dans ce dépôt GitHub. « Installer » télécharge le .zip, vérifie son sha256, puis le décompresse dans `plugins/<id>/` du bot. « Mettre à jour » apparaît quand le catalogue a une version plus récente.
- **Téléverser un plugin** : un .zip qui contient `plugin.json` et `__init__.py`. Installe juste du code que tu connais : il roule avec le jeton de ton bot.
- **Variables** : un plugin peut demander des variables (ex. une clé d'API). Tu les remplis dans sa fiche ; elles sont gardées avec les autres variables du bot, et les secrets sont jamais réaffichés.
- Redémarre le bot après avoir installé, mis à jour ou retiré un plugin. Les dépendances pip de chaque plugin (`plugins/<id>/requirements.txt`) s'installent au démarrage.

Le code d'un plugin roule seulement dans le conteneur du bot, jamais dans le panel. Le bot doit **supporter les plugins Bot Panel** : c'est lui qui les charge (les bots créés avec le modèle **Bot NG** le font, voir `templates/bot-ng/botng/plugins.py`). Le panel montre si chaque plugin est chargé, d'après le fichier `.botpanel/host.json` que le bot écrit à son démarrage.

Variables du panel : `PLUGIN_CATALOG` (adresse de l'`index.json`, vide = seulement le téléversement) et `MAX_PLUGIN_MB` (5 par défaut).

#### Écrire un plugin

Un plugin, c'est un dossier `plugins/<id>/` :

| Fichier | Contenu |
|---|---|
| `plugin.json` | la description du plugin (voir plus bas) |
| `__init__.py` | une extension discord.py : `async def setup(bot)` |
| `requirements.txt` | facultatif : des paquets PyPI, une ligne chacun (pas d'URL ni d'option pip) |

```json
{
  "format": 1,
  "id": "salut",
  "name": "Salut",
  "version": "1.0.0",
  "description": "Dit bonjour aux nouveaux.",
  "author": "toi",
  "runtime": "discord.py",
  "enabled_by": "salut.active",
  "variables": [{"key": "SALUT_CLE", "label": "Clé d'API", "secret": true, "required": true, "help": "…"}],
  "pages": [{"id": "salut", "section": "salut", "title": "Salut", "icon": "👋", "description": "…",
             "fields": [{"key": "active", "label": "Activé", "type": "bool", "default": false}]}]
}
```

- `id` : lettres minuscules, chiffres et `_` (c'est aussi le nom du module Python `plugins.<id>`).
- `pages` : ses pages dans « Options du bot », avec les mêmes types de champs que plus haut (`role_map` avec `keys`, `select` avec `choices`). Chaque page a une `section` : les valeurs y sont gardées par serveur Discord. Un plugin peut pas prendre une section du bot.
- `enabled_by` : le réglage qui fait apparaître ses commandes slash sur un serveur (ex. `salut.active`).
- `variables` : les variables d'environnement qu'il lit avec `os.getenv`.
- `intents` : les intents privilégiés dont il a besoin (`members`, `message_content`, `presences`). Le bot demande seulement ceux de ses plugins, et le panel dit lesquels activer dans le Developer Portal.

Dans `setup(bot)`, le plugin utilise `bot.plugin_host` :

| `bot.plugin_host.…` | Ce que ça fait |
|---|---|
| `config(guild_id, section)` | les réglages d'un serveur (ceux de ses pages) |
| `await save(guild_id, section, valeurs)` | enregistre des réglages (même d'un autre plugin) ; retourne les erreurs |
| `sections()` | les sections qui ont une page, donc un plugin installé |
| `data_dir(plugin_id)` | un dossier à lui, gardé quand le plugin est mis à jour ou retiré |
| `await open_db(plugin_id, schema)` | une base SQLite à lui (dans ce dossier), avec `one`, `all`, `run`, `get_value`, `set_value` |
| `await send_log(guild_id, embed, categorie)` | un événement pour le salon de logs (écrit par le plugin Logs s'il est installé) |
| `refresh_panel()` | redemande au bot d'envoyer son état au panel |

Les plugins se parlent par des événements discord.py (`bot.dispatch`), jamais directement : un plugin absent fait juste rien.

| Événement | Envoyé par | Écouté par |
|---|---|---|
| `botpanel_log(guild_id, embed, categorie)` | `plugin_host.send_log` | Logs |
| `ng_linked(member)` | Synchro NationsGlory (un `/lier` accepté) | Bienvenue & accès (retire le rôle d'arrivée) |
| `automod_warn(member, moderateur_id, raison)` | Auto-modération | Modération (garde l'avertissement) |
| `ng_country_update(guild_id, pays, membres)` | Synchro NationsGlory (après chaque synchro) | Salons compteurs (membres du pays) |

Et ses Cogs peuvent avoir ces méthodes, que le bot appelle :

| Méthode | Quand |
|---|---|
| `validate_guild_config(config) -> list[str]` | avant d'enregistrer des réglages : les erreurs bloquent |
| `async before_panel_save(guild, section, values)` | avant d'enregistrer depuis le panel : nettoyer `values`, ou lever `ValueError` pour refuser |
| `panel_info(guild_id) -> [(titre, texte)]` | lignes affichées à côté du serveur dans « Options du bot » |
| `async on_guild_config_update(guild_id)` | les réglages de ce serveur ont changé |

Une exception avec un attribut `user_message` est montrée telle quelle au membre qui a fait la commande.

#### Ajouter un plugin au catalogue

1. Mets ton plugin dans `plugins/<id>/` de ce dépôt, avec ses tests dans `tests/`.
2. Lance `python scripts/build_catalog.py` : ça crée `plugins/dist/<id>-<version>.zip` et met `plugins/index.json` à jour (les zips sont reproductibles).
3. Ouvre une pull request. Les tests vérifient que le catalogue est à jour.

Pour sortir une nouvelle version, monte `version` dans `plugin.json` et refais l'étape 2.

## Mettre le panel sur Internet

Passe par ton proxy (HAProxy, Nginx Proxy Manager…) avec un certificat HTTPS. Le proxy doit laisser passer les WebSocket, sinon la console marchera pas.

Ensuite, mets `PUBLIC_URL=https://panel.ton-domaine.duckdns.org` dans `.env` et la même adresse plus `/auth/callback` dans les Redirects de l'application Discord. Puis relance : `sudo docker compose up -d`.

Avec HTTPS, les cookies sont marqués « Secure » tout seuls.

## Dépannage

| Problème | Solution |
|---|---|
| « Docker est pas joignable » | Le volume `/var/run/docker.sock` manque dans `docker-compose.yml`. |
| Connexion Discord : « redirect_uri invalide » | La Redirect de l'application Discord doit être exactement `PUBLIC_URL` + `/auth/callback`. |
| « Ton compte est en attente » | L'admin approuve le compte dans l'onglet Admin. Ton ID doit être dans `ADMIN_IDS` pour être admin. |
| Le bot démarre pas, erreur de dossier | Le panel trouve pas son dossier data sur la machine. Mets `HOST_DATA_DIR=/opt/bot-panel/data` dans `.env`. |
| `pip`/`npm` échoue au démarrage | Regarde la console : souvent une faute dans `requirements.txt` ou pas assez de RAM (monte-la dans Réglages). |
| Plugin « pas encore dit s'il supporte les plugins » | Le bot a pas écrit `.botpanel/host.json` : démarre-le. S'il le fait jamais, ce bot supporte pas les plugins Bot Panel. |
| Plugin en erreur | La raison est sur sa fiche (onglet Plugins) et dans la console du bot. |
| Le constructeur dit « le bot a pas la permission » | Donne le rôle Administrateur au bot, et mets son rôle tout en haut de la liste des rôles. |

## Développement

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
sudo -E .venv/bin/python -m pytest -q    # en root : les fichiers des bots appartiennent à l'utilisateur 1000
```

Les tests utilisent un faux Discord en mémoire et de vrais conteneurs Docker. L'image se choisit avec `BOTPANEL_TEST_IMAGE`, par défaut `python:3.12-slim` dans GitHub Actions.

Le code est dans `botpanel/` :

| Fichier | Rôle |
|---|---|
| `web.py` | API et surveillance |
| `runtime.py` | Docker |
| `files.py` | fichiers sécuritaires |
| `gitops.py` | GitHub |
| `builder.py` | serveurs Discord |
| `auth.py` | connexion |
| `static/` | interface sans framework |

La police est Recursive, sous licence OFL.

Licence : MIT.
