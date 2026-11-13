"""/adminbuild : construit les rôles, catégories et salons du serveur, puis règle le bot dessus.

Rien n'est jamais supprimé ni modifié : ce qui existe déjà (même nom) est gardé tel quel et réutilisé.
Le modèle par défaut est fait pour un serveur de pays NationsGlory. On peut donner son propre modèle
(fichier .yml ou .json, même format que le constructeur de Bot Panel, avec « bind » en plus).
"""
from __future__ import annotations

import logging
import re
from typing import Any

import discord
import yaml
from discord import app_commands
from discord.ext import commands

from .common import BLUE, GREEN, RED

log = logging.getLogger("plugin.adminbuild")

# Plugin du catalogue qui se sert de chaque section de réglages
PLUGIN_OF = {"bienvenue": "Bienvenue & accès", "arrivee": "Bienvenue & accès", "tickets": "Tickets", "niveaux": "Niveaux",
             "logs": "Logs", "automod": "Auto-modération", "moderation": "Modération", "ng": "Synchro NationsGlory"}

EVERYONE = "@everyone"
CHANNEL_KINDS = {"text": discord.ChannelType.text, "voice": discord.ChannelType.voice,
                 "forum": discord.ChannelType.forum, "stage": discord.ChannelType.stage_voice}
NOT_VERIFIED = {"Non vérifié": {"deny": ["view_channel"]}}

# « bind » : quel réglage du bot reçoit ce rôle ou ce salon (section.clé, ou section.clé.sous-clé)
GRADES = ["Chef", "Officier", "Membre", "Recrue"]
COUNTRY_SEE = {"Citoyen": {"allow": ["view_channel"]}, **{g: {"allow": ["view_channel"]} for g in GRADES}}
DEFAULT_TEMPLATE: dict[str, Any] = {
    "roles": [  # du plus haut au plus bas
        {"name": "Staff", "color": "#e67e22", "hoist": True,
         "permissions": ["manage_messages", "kick_members", "moderate_members", "manage_nicknames", "view_audit_log", "move_members"],
         "bind": ["tickets.role_staff_id", "ng.role_validateur_id"]},
        {"name": "Chef", "color": "#e0457b", "hoist": True, "bind": ["ng.grades.LEADER"]},
        {"name": "Officier", "color": "#9b59b6", "hoist": True, "bind": ["ng.grades.OFFICER"]},
        {"name": "Membre", "color": "#3498db", "hoist": True, "bind": ["ng.grades.MEMBER"]},
        {"name": "Recrue", "color": "#2ecc71", "hoist": True, "bind": ["ng.grades.RECRUIT"]},
        {"name": "Citoyen", "bind": ["ng.role_pays_id"]},
        {"name": "Allié", "color": "#95a5a6", "hoist": True, "bind": ["ng.role_hors_pays_id"]},
        {"name": "Vérifié", "bind": ["arrivee.role_verifie_id"]},
        {"name": "Non vérifié", "bind": ["arrivee.role_id"]},
    ],
    "categories": [
        {"name": "📌 Accueil", "overwrites": {EVERYONE: {"deny": ["send_messages"]}}, "channels": [
            {"name": "👋・bienvenue", "bind": ["bienvenue.salon_id"]},
            {"name": "📜・règles", "topic": "Lis les règles avant de parler"},
            {"name": "📢・annonces", "topic": "Les nouvelles du pays", "bind": ["ng.salon_annonces_id"]},
            {"name": "🔗・liaison", "topic": "Tape /lier ton-pseudo-NationsGlory pour avoir accès au serveur",
             "overwrites": {EVERYONE: {"allow": ["send_messages"]}}},
        ]},
        {"name": "🌍 NationsGlory", "overwrites": {EVERYONE: {"deny": ["send_messages"]}, **NOT_VERIFIED}, "channels": [
            {"name": "📊・stats-ng", "topic": "Les membres du pays par grade et la fiche du pays", "bind": ["ng.salon_stats_id"]},
        ]},
        {"name": "🏰 Le pays", "private": True, "overwrites": COUNTRY_SEE, "channels": [
            {"name": "🏰・pays", "topic": "Entre membres du pays"},
            {"name": "🗺️・stratégie", "topic": "Plans, constructions, guerres",
             "overwrites": {"Citoyen": {"deny": ["view_channel"]}, "Recrue": {"deny": ["view_channel"]},
                            "Membre": {"deny": ["view_channel"]}}},
            {"name": "💰・commerce", "topic": "Achats, ventes et échanges"},
            {"name": "🔊 Pays", "type": "voice"},
            {"name": "⚔️ Guerre", "type": "voice"},
        ]},
        {"name": "💬 Discussion", "overwrites": NOT_VERIFIED, "channels": [
            {"name": "💬・général", "bind": ["niveaux.salon_annonce_id"]},
            {"name": "🤝・alliés", "topic": "Pour parler avec nos alliés"},
            {"name": "🤖・commandes", "topic": "Les commandes des bots"},
            {"name": "📸・médias", "slowmode": 10},
            {"name": "😂・memes"},
        ]},
        {"name": "🔊 Vocaux", "overwrites": NOT_VERIFIED, "channels": [
            {"name": "🔊 Général", "type": "voice"},
            {"name": "⛏️ NationsGlory 1", "type": "voice"},
            {"name": "⛏️ NationsGlory 2", "type": "voice"},
            {"name": "🎮 Jeux 1", "type": "voice"},
            {"name": "🎮 Jeux 2", "type": "voice"},
            {"name": "🎵 Musique", "type": "voice"},
            {"name": "💤 AFK", "type": "voice"},
        ]},
        {"name": "🎫 Tickets", "private": True, "overwrites": {"Staff": {"allow": ["view_channel"]}},
         "bind": ["tickets.categorie_id"], "channels": []},
        {"name": "🛡️ Staff", "private": True, "overwrites": {"Staff": {"allow": ["view_channel"]}}, "channels": [
            {"name": "🛡️・staff"},
            {"name": "✅・validation", "topic": "Les /lier à accepter ou refuser", "bind": ["ng.salon_validation_id"]},
            {"name": "📋・logs", "bind": ["logs.salon_id"]},
            {"name": "🗂️・transcriptions", "bind": ["tickets.salon_transcripts_id"]},
            {"name": "🔒 Réunion", "type": "voice"},
        ]},
    ],
}


class TemplateError(ValueError):
    pass


def norm(name: str) -> str:
    """Nom comparable : sans emojis, séparateurs ni espaces. « 👋・bienvenue » et « Bienvenue » donnent « bienvenue »."""
    return re.sub(r"[\W_]+", "", str(name).lower())


def same_kind(channel, kind: str) -> bool:
    return channel.type == CHANNEL_KINDS.get(kind, discord.ChannelType.text) or (
        kind == "text" and channel.type == discord.ChannelType.news)


def perms(names: Any, where: str) -> dict[str, bool]:
    if not names:
        return {}
    if not isinstance(names, list):
        raise TemplateError(f"{where} : les permissions doivent être une liste")
    out = {}
    for name in names:
        key = str(name).strip().lower()
        if key not in discord.Permissions.VALID_FLAGS:
            raise TemplateError(f"{where} : permission inconnue « {name} »")
        out[key] = True
    return out


def parse(text: str) -> dict:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise TemplateError(f"Fichier illisible : {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("categories", []), list) or not isinstance(data.get("roles", []), list):
        raise TemplateError("Le modèle doit avoir « roles » et/ou « categories » (format du constructeur de Bot Panel)")
    for i, r in enumerate(data.get("roles") or []):
        if not isinstance(r, dict) or not str(r.get("name", "")).strip():
            raise TemplateError(f"roles › n° {i + 1} : il manque le nom")
        perms(r.get("permissions"), f"rôle « {r['name']} »")
    def check_overwrites(item: dict, where: str) -> None:
        for target, rule in (item.get("overwrites") or {}).items():
            if not isinstance(rule, dict):
                raise TemplateError(f"{where} › {target} : il faut allow et/ou deny")
            perms(rule.get("allow"), f"{where} › {target}")
            perms(rule.get("deny"), f"{where} › {target}")

    for c in data.get("categories") or []:
        if not isinstance(c, dict) or not str(c.get("name", "")).strip():
            raise TemplateError("Une catégorie a pas de nom")
        check_overwrites(c, f"catégorie « {c['name']} »")
        for ch in c.get("channels") or []:
            ch = {"name": ch} if isinstance(ch, str) else ch
            if not isinstance(ch, dict) or not str(ch.get("name", "")).strip():
                raise TemplateError(f"Catégorie « {c['name']} » : un salon a pas de nom")
            if str(ch.get("type", "text")) not in CHANNEL_KINDS:
                raise TemplateError(f"Salon « {ch.get('name')} » : type inconnu (text, voice, forum, stage)")
            check_overwrites(ch, f"salon « {ch['name']} »")
    return data


class BuildView(discord.ui.View):
    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=180)
        self.author_id = author_id
        self.choice: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(label="Construire", style=discord.ButtonStyle.success)
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.choice = True
        await interaction.response.edit_message(content="⏳ Construction en cours…", embed=None, view=None)
        self.stop()

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.choice = False
        await interaction.response.edit_message(content="Annulé : rien a été touché.", embed=None, view=None)
        self.stop()


class Builder(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.host = bot.plugin_host  # API du bot hôte (Bot Panel)

    # ---- Plan --------------------------------------------------------------------------------------
    @staticmethod
    def match(guild: discord.Guild, tpl: dict) -> dict:
        """Ce qui existe déjà pour chaque élément du modèle (comparaison des noms sans emojis).
        Un salon est cherché dans sa catégorie, puis ailleurs sur le serveur (il sera déplacé)."""
        roles = {r.name.lower(): r for r in guild.roles}
        cats = {norm(c.name): c for c in guild.categories}
        used: set[int] = set()
        result = {"roles": {}, "categories": {}, "channels": {}}
        for i, r in enumerate(tpl.get("roles") or []):
            result["roles"][i] = roles.get(str(r["name"]).strip().lower())
        others = [c for c in guild.channels if not isinstance(c, discord.CategoryChannel)]
        for ci, c in enumerate(tpl.get("categories") or []):
            cat = cats.get(norm(c["name"]))
            result["categories"][ci] = cat
            for chi, ch in enumerate(c.get("channels") or []):
                ch = {"name": ch} if isinstance(ch, str) else ch
                kind, key = str(ch.get("type", "text")), norm(ch["name"])
                pool = [x for x in others if x.id not in used and same_kind(x, kind) and norm(x.name) == key]
                found = next((x for x in pool if cat is not None and x.category_id == cat.id), None) or next(iter(pool), None)
                if found is not None:
                    used.add(found.id)
                result["channels"][(ci, chi)] = found
        result["extra"] = [x for x in others if x.id not in used]
        return result

    @classmethod
    def plan(cls, guild: discord.Guild, tpl: dict) -> dict:
        m = cls.match(guild, tpl)
        cats = tpl.get("categories") or []
        chans = [((ci, chi), {"name": ch} if isinstance(ch, str) else ch) for ci, c in enumerate(cats)
                 for chi, ch in enumerate(c.get("channels") or [])]
        new_roles = [r for i, r in enumerate(tpl.get("roles") or []) if m["roles"][i] is None]
        new_cats = [c for ci, c in enumerate(cats) if m["categories"][ci] is None]
        new_channels = [ch for key, ch in chans if m["channels"][key] is None]
        renamed = sum(1 for ci, c in enumerate(cats) if m["categories"][ci] is not None and m["categories"][ci].name != c["name"])
        renamed += sum(1 for key, ch in chans if m["channels"][key] is not None and (
            m["channels"][key].name != cls.discord_name(ch) or m["channels"][key].category_id != getattr(m["categories"][key[0]], "id", None)))
        kept = sum(1 for v in m["roles"].values() if v) + sum(1 for v in m["categories"].values() if v) + \
            sum(1 for v in m["channels"].values() if v)
        return {"roles": new_roles, "categories": new_cats, "channels": new_channels, "kept": kept,
                "renamed": renamed, "extra": m["extra"]}

    @staticmethod
    def discord_name(ch: dict) -> str:
        """Le nom tel que Discord l'affiche (les salons texte sont en minuscules avec des tirets)."""
        name = str(ch["name"]).strip()
        return re.sub(r"\s+", "-", name.lower()) if str(ch.get("type", "text")) in ("text", "forum") else name

    # ---- Commande ----------------------------------------------------------------------------------
    @app_commands.command(name="adminbuild", description="Construit les rôles, catégories et salons du serveur et règle le bot dessus")
    @app_commands.describe(
        pays="Ton pays NationsGlory (ex. Amour) : active aussi la synchro des grades",
        serveur_ng="Serveur NationsGlory (jade par défaut)",
        fichier="Ton propre modèle (.yml ou .json, format du constructeur de Bot Panel)",
        activer="Active aussi la bienvenue et les tickets (oui par défaut)",
        renommer="Renomme et range les salons qui existent déjà (ex. #bienvenue devient #👋・bienvenue) — oui par défaut",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def adminbuild(self, interaction: discord.Interaction, pays: str | None = None, serveur_ng: str = "jade",
                         fichier: discord.Attachment | None = None, activer: bool = True, renommer: bool = True) -> None:
        guild = interaction.guild
        me = guild.me
        if not (me.guild_permissions.administrator or (me.guild_permissions.manage_roles and me.guild_permissions.manage_channels)):
            await interaction.response.send_message(
                "❌ Il me faut la permission **Administrateur** (ou Gérer les rôles + Gérer les salons) pour construire le serveur.", ephemeral=True)
            return
        try:
            if fichier is not None:
                if fichier.size > 200_000:
                    raise TemplateError("Fichier trop gros (200 Ko max)")
                tpl = parse((await fichier.read()).decode("utf-8", errors="replace"))
            else:
                tpl = DEFAULT_TEMPLATE
        except TemplateError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        p = self.plan(guild, tpl)
        lines = [
            f"**{len(p['roles'])}** rôle(s), **{len(p['categories'])}** catégorie(s) et **{len(p['channels'])}** salon(s) à créer.",
            f"{p['kept']} élément(s) existent déjà : je les réutilise.",
        ]
        if renommer and p["renamed"]:
            lines.append(f"{p['renamed']} salon(s) ou catégorie(s) existants à renommer ou ranger comme dans le modèle.")
        lines.append("Rien n'est supprimé.")
        if not fichier:
            lines.append("\nEnsuite je règle le bot : salons de bienvenue, logs, tickets, validation des /lier, salons NG et rôles des grades.")
        if pays:
            lines.append(f"Synchro NG : pays **{pays}** sur **{serveur_ng}**.")
        embed = discord.Embed(title="🏗️ Construire le serveur", description="\n".join(lines), color=BLUE)
        if p["roles"]:
            embed.add_field(name="Rôles", value=", ".join(r["name"] for r in p["roles"])[:1000], inline=False)
        if p["categories"] or p["channels"]:
            embed.add_field(name="Catégories et salons",
                            value=", ".join([f"📁 {c['name']}" for c in p["categories"]] + [f"#{c['name']}" for c in p["channels"]])[:1000],
                            inline=False)
        view = BuildView(interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.wait()
        if not view.choice:
            if view.choice is None:
                await interaction.edit_original_response(content="Temps écoulé : rien a été touché.", embed=None, view=None)
            return

        created, binds, errors = await self.build(guild, tpl, interaction, reorganize=renommer)
        configured = await self.configure(guild, binds, pays, serveur_ng, activer, errors)
        embed = discord.Embed(title="✅ Serveur construit" if not errors else "⚠️ Construit, avec des erreurs",
                              color=GREEN if not errors else RED)
        embed.description = (f"Créé : {created['roles']} rôle(s), {created['categories']} catégorie(s), {created['channels']} salon(s)."
                             + (f"\nRenommé ou rangé : {created['renamed']}." if created["renamed"] else ""))
        if configured:
            embed.add_field(name="Bot réglé", value=", ".join(configured)[:1000], inline=False)
        if errors:
            embed.add_field(name="Erreurs", value="\n".join(errors)[:1000], inline=False)
        extra = self.match(guild, tpl)["extra"]
        if extra:
            names = ", ".join(f"#{c.name}" if not isinstance(c, discord.VoiceChannel) else f"🔊 {c.name}" for c in extra[:20])
            embed.add_field(name="Pas dans le modèle (gardés : supprime-les si tu en veux plus)", value=names[:1000], inline=False)
        embed.set_footer(text="Mets le rôle du bot tout en haut de la liste des rôles pour qu'il puisse donner les grades.")
        await interaction.edit_original_response(content=None, embed=embed, view=None)
        log.info("%s a construit le serveur %s : %s", interaction.user, guild.name, created)

    # ---- Construction ---------------------------------------------------------------------------------
    async def build(self, guild: discord.Guild, tpl: dict, interaction: discord.Interaction, *, reorganize: bool = True):
        reason = f"/adminbuild par {interaction.user}"
        created = {"roles": 0, "categories": 0, "channels": 0, "renamed": 0}
        binds: dict[str, int] = {}
        errors: list[str] = []
        m = self.match(guild, tpl)
        roles = {r.name.lower(): r for r in guild.roles}

        def bind(item: dict, obj_id: int) -> None:
            for key in item.get("bind") or []:
                binds[str(key)] = obj_id

        # Rôles : du plus bas au plus haut, comme ça le premier de la liste finit en haut
        tpl_roles = tpl.get("roles") or []
        for i in reversed(range(len(tpl_roles))):
            r = tpl_roles[i]
            name = str(r["name"]).strip()
            role = m["roles"][i]
            if role is None:
                try:
                    color = discord.Colour(int(str(r.get("color", "0")).lstrip("#") or "0", 16))
                    role = await guild.create_role(name=name, permissions=discord.Permissions(**perms(r.get("permissions"), name)),
                                                   colour=color, hoist=bool(r.get("hoist")),
                                                   mentionable=bool(r.get("mentionable")), reason=reason)
                    roles[name.lower()] = role
                    created["roles"] += 1
                except (discord.HTTPException, ValueError) as e:
                    errors.append(f"Rôle « {name} » : {e}")
                    continue
            bind(r, role.id)

        def overwrites(spec: dict | None, private: bool) -> dict:
            out: dict[Any, discord.PermissionOverwrite] = {}
            if private:
                out[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
            for target, rule in (spec or {}).items():
                obj = guild.default_role if target in (EVERYONE, "everyone") else roles.get(str(target).lower())
                if obj is None:
                    errors.append(f"Rôle « {target} » introuvable pour les permissions")
                    continue
                ow = out.get(obj, discord.PermissionOverwrite())
                for p in (rule or {}).get("allow") or []:
                    setattr(ow, p, True)
                for p in (rule or {}).get("deny") or []:
                    setattr(ow, p, False)
                out[obj] = ow
            return out

        for ci, c in enumerate(tpl.get("categories") or []):
            name = str(c["name"]).strip()
            cat_ow = overwrites(c.get("overwrites"), bool(c.get("private")))
            category = m["categories"][ci]
            try:
                if category is None:
                    category = await guild.create_category(name, overwrites=cat_ow, reason=reason)
                    created["categories"] += 1
                elif reorganize and category.name != name:
                    await category.edit(name=name, reason=reason)
                    created["renamed"] += 1
            except discord.HTTPException as e:
                errors.append(f"Catégorie « {name} » : {e}")
                if category is None:
                    continue
            bind(c, category.id)
            for chi, ch in enumerate(c.get("channels") or []):
                ch = {"name": ch} if isinstance(ch, str) else ch
                kind = str(ch.get("type", "text"))
                ch_name = str(ch["name"]).strip()
                channel = m["channels"][(ci, chi)]
                try:
                    if channel is None:
                        ow = {**cat_ow, **overwrites(ch.get("overwrites"), bool(ch.get("private")))}
                        if kind == "voice":
                            channel = await category.create_voice_channel(ch_name, overwrites=ow, reason=reason)
                        elif kind == "stage":
                            channel = await category.create_stage_channel(ch_name, overwrites=ow, reason=reason)
                        elif kind == "forum":
                            channel = await category.create_forum(ch_name, overwrites=ow, reason=reason)
                        else:
                            channel = await category.create_text_channel(
                                ch_name, overwrites=ow, topic=ch.get("topic") or None,
                                slowmode_delay=int(ch.get("slowmode") or 0), reason=reason)
                        created["channels"] += 1
                    elif reorganize:
                        changes: dict[str, Any] = {}
                        if channel.name != self.discord_name(ch):
                            changes["name"] = ch_name
                        if channel.category_id != category.id:
                            changes.update(category=category, sync_permissions=True)
                        if changes:
                            await channel.edit(reason=reason, **changes)
                            created["renamed"] += 1
                except discord.HTTPException as e:
                    errors.append(f"Salon « {ch_name} » : {e}")
                    if channel is None:
                        continue
                bind(ch, channel.id)
        return created, binds, errors

    # ---- Réglages du bot ---------------------------------------------------------------------------------
    async def configure(self, guild: discord.Guild, binds: dict[str, int], pays: str | None, serveur_ng: str,
                        activer: bool, errors: list[str]) -> list[str]:
        sections: dict[str, dict] = {}
        for path, obj_id in binds.items():
            parts = path.split(".")
            if len(parts) == 2:
                sections.setdefault(parts[0], {})[parts[1]] = obj_id
            elif len(parts) == 3:  # ex. ng.grades.LEADER
                current = dict(self.host.config(guild.id, parts[0]).get(parts[1]) or {})
                sub = sections.setdefault(parts[0], {}).setdefault(parts[1], current)
                sub[parts[2]] = obj_id
        if activer:
            if "bienvenue" in sections:
                sections["bienvenue"]["active"] = True
            if "tickets" in sections:
                sections["tickets"]["active"] = True
        if pays:
            sections.setdefault("ng", {}).update(pays=pays.strip(), serveur=serveur_ng.strip().lower() or "jade", active=True)
        done = []
        labels = {"bienvenue": "bienvenue", "arrivee": "arrivée", "tickets": "tickets", "niveaux": "niveaux",
                  "logs": "logs", "ng": "NationsGlory"}
        for section, values in sections.items():
            try:
                failures = await self.host.save(guild.id, section, values)
            except Exception as e:  # noqa: BLE001
                failures = [str(e)]
            if failures:
                errors.append(f"Réglages {labels.get(section, section)} : " + " · ".join(failures))
            else:
                done.append(labels.get(section, section))
        missing = [PLUGIN_OF.get(s, s) for s in sections if s not in self.host.sections()]
        if missing:
            # Les réglages sont gardés : ils serviront dès que le plugin sera installé
            errors.append("Plugins à installer (onglet Plugins de Bot Panel) pour que ces réglages servent : "
                          + ", ".join(f"« {m} »" for m in sorted(set(missing))))
        self.host.refresh_panel()
        return done


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Builder(bot))
