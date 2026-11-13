"""Rôles à boutons : un panneau où les membres prennent et enlèvent eux-mêmes leurs rôles."""
from __future__ import annotations

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from .common import BLUE, reply, truncate

log = logging.getLogger("plugin.roles")

PANELS = ("1", "2", "3")  # trois panneaux possibles par serveur (ex. alertes, jeux, notifications)


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rb:(?P<panel>[123]):(?P<role_id>\d+)"):
    def __init__(self, panel: str, role: discord.Role | None = None, role_id: int = 0) -> None:
        self.panel, self.role_id = panel, role.id if role else role_id
        super().__init__(discord.ui.Button(label=truncate(role.name, 80) if role else "Rôle", style=discord.ButtonStyle.secondary,
                                           custom_id=f"rb:{panel}:{self.role_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["panel"], role_id=int(match["role_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("ButtonRoles")
        if cog is None:
            return await reply(interaction, "Les rôles à boutons sont désactivés.")
        await cog.toggle(interaction, self.panel, self.role_id)


class ButtonRoles(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.host = bot.plugin_host  # API du bot hôte (Bot Panel)

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(RoleButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(RoleButton)

    def panel_cfg(self, guild_id: int, panel: str) -> dict:
        cfg = self.host.config(guild_id, "roles")
        return {key: cfg.get(f"{key}_{panel}") for key in ("titre", "message", "roles", "unique")}

    @staticmethod
    def usable(guild: discord.Guild, role_id: int) -> discord.Role | None:
        role = guild.get_role(int(role_id or 0))
        if role is None or role.managed or role.is_default() or role >= guild.me.top_role:
            return None
        return role

    async def toggle(self, interaction: discord.Interaction, panel: str, role_id: int) -> None:
        guild = interaction.guild
        member = interaction.user if hasattr(interaction.user, "roles") else None  # un Member sur un serveur
        if guild is None or member is None:
            return await reply(interaction, "Ça marche juste sur un serveur.")
        cfg = self.panel_cfg(guild.id, panel)
        allowed = [int(r) for r in cfg["roles"] or []]
        if role_id not in allowed:  # retiré des options depuis la publication du panneau
            return await reply(interaction, "Ce rôle est plus offert ici. Demande au staff de republier le panneau.")
        role = self.usable(guild, role_id)
        if role is None:
            return await reply(interaction, "⚠️ Je peux pas donner ce rôle : il faut monter le rôle du bot au-dessus.")
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Rôles à boutons")
                return await reply(interaction, f"➖ Rôle **{role.name}** enlevé.")
            others = []
            if cfg["unique"]:  # un seul rôle de ce panneau à la fois
                others = [r for r in member.roles if r.id in allowed and r.id != role.id]
                if others:
                    await member.remove_roles(*others, reason="Rôles à boutons (un seul à la fois)")
            await member.add_roles(role, reason="Rôles à boutons")
        except discord.HTTPException as e:
            log.warning("%s : rôle %s pas modifié (%s)", guild.name, role.name, e)
            return await reply(interaction, "⚠️ Discord a refusé : vérifie que le rôle du bot est au-dessus.")
        swapped = f" (au lieu de {', '.join(r.name for r in others)})" if others else ""
        await reply(interaction, f"➕ Rôle **{role.name}** ajouté{swapped}.")

    @app_commands.command(name="roles-panneau", description="Publie un panneau de rôles à boutons dans ce salon")
    @app_commands.describe(panneau="Lequel des panneaux réglés dans les options du bot")
    @app_commands.choices(panneau=[app_commands.Choice(name=f"Panneau {p}", value=p) for p in PANELS])
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def panneau(self, interaction: discord.Interaction, panneau: app_commands.Choice[str] | None = None) -> None:
        panel = panneau.value if panneau else "1"
        guild = interaction.guild
        cfg = self.panel_cfg(guild.id, panel)
        roles = [r for r in (self.usable(guild, rid) for rid in cfg["roles"] or []) if r is not None][:25]
        if not roles:
            return await reply(interaction, f"Choisis des rôles pour le panneau {panel} dans les options du bot "
                                            "(et monte le rôle du bot au-dessus d'eux).")
        embed = discord.Embed(title=truncate(str(cfg["titre"] or "Choisis tes rôles"), 256),
                              description=truncate(str(cfg["message"] or ""), 4096) or None, color=BLUE)
        if cfg["unique"]:
            embed.set_footer(text="Un seul rôle à la fois. Reclique pour l'enlever.")
        else:
            embed.set_footer(text="Clique pour prendre un rôle, reclique pour l'enlever.")
        view = discord.ui.View(timeout=None)
        for role in roles:
            view.add_item(RoleButton(panel, role))
        await interaction.channel.send(embed=embed, view=view)
        await reply(interaction, f"Panneau {panel} publié ({len(roles)} rôle(s)).")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ButtonRoles(bot))
