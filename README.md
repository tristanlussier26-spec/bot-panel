# Bot Panel

Un panel web pour héberger des bots Discord, un peu comme Pterodactyl, mais fait juste pour ça. Tu peux aussi y monter des serveurs Discord à partir d'un modèle.

- **Héberger n'importe quel bot** en Python, en Node.js ou avec une image Docker de ton choix. Chaque bot roule dans son propre conteneur, avec ses limites de RAM et de CPU.
- **Console en direct** : tu vois ce que ton bot écrit et tu peux lui envoyer des commandes. Tu as aussi les boutons démarrer, redémarrer, arrêter et tuer, et la RAM et le CPU s'affichent en temps réel.
- **Fichiers** : éditeur dans le navigateur, téléversement par glisser-déposer, décompression de .zip et téléchargement.
- **Déploiement depuis GitHub** : clone ton dépôt et mets-le à jour en un clic. Les dépôts privés marchent avec un jeton.
- **Variables d'environnement** : ton `DISCORD_TOKEN` est caché et jamais réaffiché.
- **Redémarrage automatique** si le bot plante. S'il plante en boucle, le panel arrête d'essayer et te le dit dans la console.
- **Constructeur de serveurs Discord** : tu décris les rôles, catégories, salons et permissions dans un modèle, tu vois le plan, puis tu l'appliques avec un de tes bots. Tu peux aussi copier un serveur existant ou importer un modèle discord.new.
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
