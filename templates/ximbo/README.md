# Ximbo 🎧

Bot musique à créer dans Bot Panel (**Nouveau bot › Modèle : Ximbo**). Un membre clique sur **« Créer mon bot musique »** et reçoit un **salon privé** pour contrôler le bot : il y écrit un titre ou colle un lien Spotify, et Ximbo joue dans son vocal.

C'est un **Bot NG** (dossier `botng/`) avec le plugin **ximbo** installé d'office. Tu peux lui ajouter d'autres plugins dans l'onglet Plugins.

## Démarrer

1. [Developer Portal](https://discord.com/developers/applications) › ton application :
   - **Bot › Reset Token**, copie le jeton ;
   - **Bot › Privileged Gateway Intents** : active **Message Content Intent** (pour lire les titres écrits dans les salons).
2. Spotify (facultatif mais conseillé) : [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) › **Create app** (Redirect URI : `http://127.0.0.1`), copie le **Client ID** et le **Client secret**.
3. Bot Panel › **Nouveau bot** › modèle **Ximbo**, avec **1024 Mo de RAM** au moins (Lavalink roule en Java dans le bot).
4. Onglet **Variables** : `DISCORD_TOKEN`. Onglet **Plugins › Ximbo** : `XIMBO_SPOTIFY_ID` et `XIMBO_SPOTIFY_SECRET`.
5. Démarre le bot. **Le premier démarrage prend quelques minutes** : il télécharge Java et Lavalink (~200 Mo, une seule fois). `/ximbo-statut` montre où il en est.
6. Invite le bot avec les permissions : Voir les salons, Envoyer des messages, Intégrer des liens, Gérer les messages, Lire l'historique, **Gérer les salons**, **Gérer les rôles**, Se connecter, Parler.
7. **Options du bot › Ximbo** : coche « Ximbo activé », puis `/ximbo-panneau` dans le salon où tu veux le bouton.

## Ce qu'il fait

- Un salon de contrôle privé par membre, avec un lecteur à jour en direct (pochette, progression, 5 prochains titres)
- Titres, playlists et albums **Spotify**, YouTube, YouTube Music, SoundCloud, Twitch, Bandcamp, liens audio directs
- Boutons : ⏮️ ⏯️ ⏭️ ⏹️ 🔀, volume, boucle titre/file, ✨ autoplay (recommandations), 📜 file
- Filtres : bass boost, nightcore, vaporwave, 8D, karaoké
- **👥 Inviter** des amis dans ton salon, **❌ Fermer** le salon
- Quitte le vocal quand il joue plus rien ou qu'il est seul
- Réglages par serveur (Options du bot) : catégorie des salons, rôle qui peut en créer, nombre max, recherche par défaut, volume

Un bot Discord est dans **un seul vocal par serveur** : si quelqu'un écoute déjà, les autres salons attendent qu'il soit libre.

## Bon à savoir

- Spotify diffuse pas l'audio aux bots : Ximbo prend les infos sur Spotify et joue le même titre depuis YouTube Music (ou SoundCloud en secours).
- Si YouTube bloque l'adresse de ton serveur (« Sign in to confirm you're not a bot »), redémarre le bot le lendemain : il vérifie les mises à jour du plugin YouTube deux fois par jour.
- Tout ce que Lavalink télécharge est dans `data/plugins/ximbo/lavalink/`. Efface ce dossier pour tout réinstaller.
