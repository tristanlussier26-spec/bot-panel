"""Recrutement : candidatures au pays (/postuler ou bouton), décidées par le staff avec Accepter/Refuser."""
from __future__ import annotations

import json
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from .common import BLUE, GREEN, GREY, RED, fill, make_embed, reply, truncate

log = logging.getLogger("plugin.recrutement")

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidatures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    answers     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'attente',   -- attente, acceptee, refusee
    created_at  INTEGER NOT NULL,
    decided_by  INTEGER,
    decided_at  INTEGER,
    reason      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cand_user ON candidatures (guild_id, user_id);
"""
MAX_QUESTIONS = 5  # limite de Discord pour un formulaire


def questions(cfg: dict) -> list[str]:
    return [str(q).strip() for q in cfg.get("questions") or [] if str(q).strip()][:MAX_QUESTIONS] or ["Pourquoi veux-tu nous rejoindre?"]


class ApplyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rec:postuler"):
    def __init__(self) -> None:
        super().__init__(discord.ui.Button(label="Postuler", emoji="📝", style=discord.ButtonStyle.primary, custom_id="rec:postuler"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Recruitment")
        if cog is None:
            return await reply(interaction, "Le recrutement est désactivé.")
        await cog.start_application(interaction)


class DecisionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rec:(?P<action>ok|no):(?P<app_id>\d+)"):
    def __init__(self, action: str, app_id: int) -> None:
        self.action, self.app_id = action, app_id
        super().__init__(discord.ui.Button(
            label="Accepter" if action == "ok" else "Refuser", emoji="✅" if action == "ok" else "✖️",
            style=discord.ButtonStyle.success if action == "ok" else discord.ButtonStyle.danger,
            custom_id=f"rec:{action}:{app_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["action"], int(match["app_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Recruitment")
        if cog is None:
            return await reply(interaction, "Le recrutement est désactivé.")
        if self.action == "ok":
            await cog.decide(interaction, self.app_id, accept=True)
        else:
            await interaction.response.send_modal(RefuseModal(cog, self.app_id))


class ApplicationModal(discord.ui.Modal):
    def __init__(self, cog: "Recruitment", guild: discord.Guild) -> None:
        super().__init__(title=truncate(f"Candidature : {guild.name}", 45), timeout=900)
        self.cog = cog
        self.inputs = []
        for i, question in enumerate(questions(cog.host.config(guild.id, "recrutement"))):
            field = discord.ui.TextInput(style=discord.TextStyle.short if i == 0 else discord.TextStyle.paragraph,
                                         max_length=100 if i == 0 else 1000, required=True)
            self.inputs.append((question, field))
            self.add_item(discord.ui.Label(text=truncate(question, 45), component=field,
                                           description=truncate(question, 100) if len(question) > 45 else None))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit(interaction, [(q, f.value) for q, f in self.inputs])


class RefuseModal(discord.ui.Modal, title="Refuser la candidature"):
    reason = discord.ui.Label(text="Raison (envoyée au candidat)", description="Facultatif",
                              component=discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False, max_length=500))

    def __init__(self, cog: "Recruitment", app_id: int) -> None:
        super().__init__(timeout=600)
        self.cog, self.app_id = cog, app_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.decide(interaction, self.app_id, accept=False, reason=self.reason.component.value.strip())


class Recruitment(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.host = bot.plugin_host  # API du bot hôte (Bot Panel)
        self.db = None

    async def cog_load(self) -> None:
        self.db = await self.host.open_db("recrutement", SCHEMA)
        self.bot.add_dynamic_items(ApplyButton, DecisionButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(ApplyButton, DecisionButton)

    def cfg(self, guild_id: int) -> dict:
        return self.host.config(guild_id, "recrutement")

    # ---- Candidat -----------------------------------------------------------------------------------
    async def blocked(self, guild_id: int, user_id: int) -> str | None:
        """Raison pour laquelle ce membre peut pas postuler maintenant, sinon None."""
        last = await self.db.one("SELECT * FROM candidatures WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
                                 (guild_id, user_id))
        if last is None:
            return None
        if last["status"] == "attente":
            return "📨 Ta candidature est déjà en attente. Le staff va te répondre en MP."
        if last["status"] == "acceptee":
            return "✅ Ta candidature a déjà été acceptée."
        wait_days = int(self.cfg(guild_id).get("attente_jours", 7) or 0)
        again = (last["decided_at"] or last["created_at"]) + wait_days * 86400
        if time.time() < again:
            return f"⏳ Tu pourras repostuler <t:{int(again)}:R>."
        return None

    async def start_application(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not self.cfg(guild.id).get("active"):
            return await reply(interaction, "Le recrutement est fermé sur ce serveur.")
        if not self.cfg(guild.id).get("salon_candidatures_id"):
            return await reply(interaction, "⚙️ Le salon des candidatures est pas réglé. Préviens un admin.")
        if problem := await self.blocked(guild.id, interaction.user.id):
            return await reply(interaction, problem)
        await interaction.response.send_modal(ApplicationModal(self, guild))

    async def submit(self, interaction: discord.Interaction, answers: list[tuple[str, str]]) -> None:
        guild, user = interaction.guild, interaction.user
        if problem := await self.blocked(guild.id, user.id):  # deux formulaires ouverts en même temps
            return await reply(interaction, problem)
        channel = guild.get_channel(int(self.cfg(guild.id).get("salon_candidatures_id", 0) or 0))
        if channel is None:
            return await reply(interaction, "⚙️ Le salon des candidatures est introuvable. Préviens un admin.")
        cur = await self.db.run("INSERT INTO candidatures (guild_id, user_id, answers, created_at) VALUES (?, ?, ?, ?)",
                                (guild.id, user.id, json.dumps(answers, ensure_ascii=False), int(time.time())))
        app_id = cur.lastrowid
        embed = make_embed(f"📝 Candidature n°{app_id}", BLUE, user=user,
                           description=f"{user.mention} veut rejoindre **{guild.name}**.")
        for question, answer in answers:
            embed.add_field(name=truncate(question, 256), value=truncate(answer, 1024) or "—", inline=False)
        embed.add_field(name="Compte Discord créé", value=discord.utils.format_dt(user.created_at, "R"))
        view = discord.ui.View(timeout=None)
        view.add_item(DecisionButton("ok", app_id))
        view.add_item(DecisionButton("no", app_id))
        staff_role = int(self.cfg(guild.id).get("role_staff_id", 0) or 0)
        await channel.send(content=f"<@&{staff_role}>" if staff_role else None, embed=embed, view=view,
                           allowed_mentions=discord.AllowedMentions(roles=[discord.Object(staff_role)] if staff_role else False))
        await reply(interaction, "📨 Candidature envoyée! Tu vas recevoir la réponse en MP.")

    # ---- Staff ----------------------------------------------------------------------------------------
    def can_decide(self, member: discord.Member) -> bool:
        staff_role = int(self.cfg(member.guild.id).get("role_staff_id", 0) or 0)
        if staff_role and any(r.id == staff_role for r in member.roles):
            return True
        return bool(member.guild_permissions.manage_roles)

    def _roles(self, guild: discord.Guild, ids) -> list[discord.Role]:
        roles = []
        for role_id in ids:
            role = guild.get_role(int(role_id or 0))
            if role is None or role.managed or role >= guild.me.top_role:
                if role is not None:
                    log.warning("%s : je peux pas gérer le rôle « %s » (monte le rôle du bot)", guild.name, role.name)
                continue
            roles.append(role)
        return roles

    async def decide(self, interaction: discord.Interaction, app_id: int, *, accept: bool, reason: str = "") -> None:
        guild = interaction.guild
        staff = interaction.user if isinstance(interaction.user, discord.Member) else (guild.get_member(interaction.user.id) if guild else None)
        if staff is None or not self.can_decide(staff):
            return await reply(interaction, "⛔ T'as pas le droit de décider des candidatures.")
        app = await self.db.one("SELECT * FROM candidatures WHERE id = ? AND guild_id = ?", (app_id, guild.id))
        if app is None:
            return await reply(interaction, "Candidature introuvable.")
        if app["status"] != "attente":
            return await reply(interaction, "Cette candidature a déjà été traitée.")
        await self.db.run("UPDATE candidatures SET status = ?, decided_by = ?, decided_at = ?, reason = ? WHERE id = ?",
                          ("acceptee" if accept else "refusee", staff.id, int(time.time()), reason, app_id))
        cfg = self.cfg(guild.id)
        candidate = guild.get_member(app["user_id"])
        if candidate is None:
            try:
                candidate = await guild.fetch_member(app["user_id"])
            except discord.HTTPException:
                candidate = None  # parti du serveur
        note = ""
        if accept and candidate is not None:
            try:
                if give := self._roles(guild, cfg.get("roles_acceptes") or []):
                    await candidate.add_roles(*give, reason=f"Candidature acceptée par {staff}")
                if remove := [r for r in self._roles(guild, [cfg.get("role_retire_id")]) if r in candidate.roles]:
                    await candidate.remove_roles(*remove, reason="Candidature acceptée")
            except discord.HTTPException as e:
                note = f"\n⚠️ Rôles pas donnés : {e}"
        elif candidate is None:
            note = "\n⚠️ Le candidat a quitté le serveur."
        if candidate is not None:
            text = fill(str(cfg.get("message_accepte" if accept else "message_refuse") or ""), candidate)
            if not accept and reason:
                text += f"\nRaison : {reason}"
            try:
                await candidate.send(truncate(text, 2000))
            except discord.HTTPException:
                note += "\n(MP fermés : le candidat a pas reçu la réponse.)"
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else make_embed("Candidature", GREY)
        embed.color = GREEN if accept else RED
        decision = f"{'✅ Acceptée' if accept else '✖️ Refusée'} par {staff.mention}" + (f"\nRaison : {truncate(reason, 900)}" if reason else "")
        embed.add_field(name="Décision", value=decision + note, inline=False)
        if interaction.response.is_done():
            await interaction.message.edit(embed=embed, view=None)
        else:
            await interaction.response.edit_message(embed=embed, view=None)
        await self.host.send_log(guild.id, make_embed(
            f"📝 Candidature {'acceptée' if accept else 'refusée'}", GREEN if accept else RED,
            description=f"<@{app['user_id']}> · par {staff.mention}" + (f"\nRaison : {truncate(reason, 900)}" if reason else "")), "membres")

    # ---- Commandes --------------------------------------------------------------------------------------
    @app_commands.command(name="postuler", description="Postuler pour rejoindre le pays")
    @app_commands.guild_only()
    async def postuler(self, interaction: discord.Interaction) -> None:
        await self.start_application(interaction)

    @app_commands.command(name="recrutement-panneau", description="Publie le panneau « Postuler » dans ce salon")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def panneau(self, interaction: discord.Interaction) -> None:
        cfg = self.cfg(interaction.guild.id)
        embed = discord.Embed(title=truncate(str(cfg.get("titre_panneau") or "📝 Recrutement"), 256),
                              description=truncate(str(cfg.get("message_panneau") or ""), 4096), color=BLUE)
        view = discord.ui.View(timeout=None)
        view.add_item(ApplyButton())
        await interaction.channel.send(embed=embed, view=view)
        await reply(interaction, "Panneau publié.")

    @app_commands.command(name="candidatures", description="Les candidatures en attente")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def candidatures(self, interaction: discord.Interaction) -> None:
        rows = await self.db.all("SELECT * FROM candidatures WHERE guild_id = ? AND status = 'attente' ORDER BY id",
                                 (interaction.guild.id,))
        lines = [f"n°{r['id']} · <@{r['user_id']}> · <t:{r['created_at']}:R>" for r in rows[:30]]
        await reply(interaction, embed=discord.Embed(title=f"📝 {len(rows)} candidature(s) en attente",
                                                     description="\n".join(lines) or "Aucune.", color=BLUE))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Recruitment(bot))
