# Bot NG

Un bot Discord pour un pays NationsGlory, à créer dans Bot Panel (**Nouveau bot › Modèle : Bot NG**). Le bot de base fait le minimum ; ce qu'il fait vraiment vient des **plugins** (onglet Plugins).

## Démarrer

1. Crée l'application sur le [Developer Portal](https://discord.com/developers/applications), puis copie le jeton (Bot › Reset Token).
2. Dans Bot Panel : **Nouveau bot**, modèle **Bot NG**, puis colle le jeton dans **Variables** sous le nom `DISCORD_TOKEN`.
3. Onglet **Plugins** : installe ce que tu veux. La fiche de chaque plugin dit quels **intents** activer dans le Developer Portal (Bot › Privileged Gateway Intents).
4. Démarre le bot, invite-le sur ton serveur, puis règle chaque plugin dans **Options du bot**.

Variable optionnelle : `BOT_STATUT` (le statut affiché sous le nom du bot).

## Ce qu'il y a dedans

| Fichier | Rôle |
|---|---|
| `bot.py` | démarre le bot |
| `botng/bot.py` | le bot : réglages par serveur, commandes par serveur, chargement des plugins |
| `botng/plugins.py` | lit `plugins/*/plugin.json` et donne `bot.plugin_host` aux plugins |
| `botng/panelbridge.py` | le lien avec l'onglet « Options du bot » de Bot Panel (fichiers dans `.botpanel/`) |
| `botng/config.py` | types de champs et validation des réglages |
| `botng/general.py` | `/aide`, `/ping`, `/serveur`, `/utilisateur`, `/avatar`, `/sondage` |

Les réglages sont dans `data/bot.db`, et chaque plugin a son dossier dans `data/plugins/<id>/`. Les mises à jour du modèle (onglet Réglages) touchent pas à ces dossiers ni aux plugins.

## Sans Bot Panel

```sh
pip install -r requirements.txt
echo "DISCORD_TOKEN=ton-jeton" > .env
python bot.py
```

Mets les plugins dans `plugins/<id>/`. Sans Bot Panel, il y a pas d'onglet « Options du bot » : les réglages restent à leurs valeurs par défaut.
