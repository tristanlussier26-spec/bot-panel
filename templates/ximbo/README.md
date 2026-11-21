# Ximbo 🎧

Bot musique à créer dans Bot Panel (**Nouveau bot › Modèle : Ximbo**). Un membre tape **`/ximbo on`** dans le clavardage de son vocal (ou clique sur **« Créer mon bot musique »**) : Ximbo le rejoint et lui ouvre un **salon privé** pour contrôler le bot. Il y écrit un titre ou colle un lien Spotify, Deezer ou YouTube (playlists et albums aussi), et peut inviter d'autres personnes à contrôler.

C'est un **Bot NG** (dossier `botng/`) avec le plugin **ximbo** installé d'office. Tu peux lui ajouter d'autres plugins dans l'onglet Plugins.

## Démarrer

1. [Developer Portal](https://discord.com/developers/applications) › ton application :
   - **Bot › Reset Token**, copie le jeton ;
   - **Bot › Privileged Gateway Intents** : active **Message Content Intent** (pour lire les titres écrits dans les salons).
2. Spotify (facultatif) : seulement pour **chercher les titres tapés sur Spotify**. Les liens Spotify (playlists, albums) marchent sans. [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) › **Create app** (Redirect URI : `http://127.0.0.1:8888/callback`, coche **Web API**), copie le **Client ID** et le **Client secret**. Depuis février 2026, le compte qui crée l'app doit avoir **Spotify Premium**.
3. Bot Panel › **Nouveau bot** › modèle **Ximbo**, avec **1024 Mo de RAM** au moins (Lavalink roule en Java dans le bot).
4. Onglet **Variables** : `DISCORD_TOKEN`. Onglet **Plugins › Ximbo** (facultatif) : `XIMBO_SPOTIFY_ID` et `XIMBO_SPOTIFY_SECRET`.
5. Démarre le bot. **Le premier démarrage prend quelques minutes** : il télécharge Java et Lavalink (~200 Mo, une seule fois). `/ximbo-statut` montre où il en est.
6. Invite le bot avec les permissions : Voir les salons, Envoyer des messages, Intégrer des liens, Gérer les messages, Lire l'historique, **Gérer les salons**, **Gérer les rôles**, Se connecter, Parler.
7. **Options du bot › Ximbo** : coche « Ximbo activé ». Ensuite, `/ximbo on` dans le clavardage de n'importe quel vocal ; `/ximbo-panneau` publie aussi le bouton dans un salon.

## Commandes

| Commande | Ce qu'elle fait |
|---|---|
| `/ximbo on [recherche]` | Dans le clavardage d'un vocal : Ximbo rejoint ce vocal, ouvre ton salon de contrôle et peut lancer un titre ou une playlist tout de suite |
| `/ximbo off [garder_salon]` | Arrête la musique et ferme ton salon (ou le garde) |
| `/ximbo inviter @membre` · `/ximbo retirer @membre` | Donne ou retire le contrôle |
| `/ximbo jouer recherche [melanger] [ensuite]` | Ajoute un titre, un album ou une playlist, avec suggestions pendant que tu tapes |
| `/ximbo file` | La file d'attente |
| `/ximbo playlist sauver · charger · liste · supprimer` | Tes playlists perso (titre en cours + file), gardées d'un serveur à l'autre |
| `/ximbo-panneau` · `/ximbo-fermer` · `/ximbo-statut` | Bouton « Créer mon bot musique », fermer son salon, état du lecteur |

Dans le clavardage du vocal, une **carte « en lecture »** montre le titre, la suite et qui contrôle, avec trois boutons pour tout le monde : **🙋 Demander le contrôle** (le propriétaire accepte ou refuse dans son salon), **⏭️ Voter pour passer** (la moitié des gens du vocal suffit ; un contrôleur passe tout de suite) et **📜 File**.

## Ce qu'il fait

- Un salon de contrôle privé par membre, avec un lecteur à jour en direct (pochette, progression, 5 prochains titres)
- Titres, playlists et albums **Spotify** et **Deezer**, YouTube, YouTube Music, SoundCloud, Twitch, Bandcamp, liens audio directs
- Boutons : ⏮️ ⏯️ ⏭️ ⏹️ 🔀, volume, boucle titre/file, ✨ autoplay (recommandations), 📜 file
- Filtres : bass boost, nightcore, vaporwave, 8D, karaoké
- **👥 Inviter** des amis à contrôler (ou ils le demandent avec 🙋), **❌ Fermer** le salon
- Playlists perso : `/ximbo playlist sauver` garde le titre en cours et la file (500 titres max, 25 playlists)
- Quitte le vocal quand il joue plus rien ou qu'il est seul
- Réglages par serveur (Options du bot) : catégorie des salons, rôle qui peut en créer, nombre max, recherche par défaut, volume

Un bot Discord est dans **un seul vocal par serveur** : si quelqu'un écoute déjà, les autres salons attendent qu'il soit libre.

## Bon à savoir

- Spotify et Deezer diffusent pas l'audio aux bots : Ximbo lit la liste des chansons (pour Spotify, par ses pages publiques, avec spotifyscraper, sans clé d'API ; l'API officielle donne plus les playlists aux nouvelles apps depuis février 2026), puis cherche chaque chanson sur YouTube **au moment de la jouer**, en prenant le résultat qui a la bonne durée. La suivante se prépare pendant que la courante joue.
- L'audio YouTube vient de **yt-dlp** (avec deno pour les défis JavaScript de YouTube, téléchargé par Ximbo au premier démarrage, ~45 Mo) : YouTube refuse l'audio au plugin youtube-source de Lavalink. Ximbo le relaie à Lavalink par un petit serveur local (`127.0.0.1:2334`, variable `XIMBO_RELAY_PORT`), alors les filtres, le volume et l'avance rapide marchent pareil. Les playlists Spotify doivent être **publiques**.
- yt-dlp se met à jour tout seul une fois par jour (YouTube change souvent) ; la nouvelle version sert au prochain redémarrage. Si YouTube se met à tout refuser, redémarre le bot.
- Une chanson introuvable ou illisible est annoncée dans le salon et Ximbo passe à la suivante.
- Tout ce que Lavalink télécharge est dans `data/plugins/ximbo/lavalink/` (yt-dlp et deno dans `data/plugins/ximbo/yt-dlp/`). Efface ces dossiers pour tout réinstaller.
