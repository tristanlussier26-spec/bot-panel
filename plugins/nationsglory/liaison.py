"""Liaison automatique des comptes Discord ↔ NationsGlory.

1. Le bot compare le surnom, le nom affiché et le nom d'utilisateur Discord aux pseudos des membres du pays.
   « [SKH] pfpf », « pfpf | Empereur » ou « PFPF » trouvent tous « pfpf ».
2. S'il trouve pas, il demande à la personne (MP, ou le salon de liaison) avec un bouton qui ouvre un formulaire.
3. Les grades élevés (chef, officiers) passent quand même par la validation, s'il y a un salon de validation :
   n'importe qui pourrait se renommer comme le chef du pays.
"""
from __future__ import annotations

import re

import discord

from .common import reply

TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,16}")
HIGH_RANKS = ("LEADER", "OFFICER")


def candidate_names(member) -> list[str]:
    """Les noms Discord d'un membre, du plus parlant (surnom du serveur) au moins parlant."""
    out = []
    for value in (getattr(member, "nick", None), getattr(member, "global_name", None), getattr(member, "name", None)):
        if value and value not in out:
            out.append(str(value))
    return out


def match_member(names: list[str], members: dict[str, dict], taken: set[str] = frozenset()) -> str | None:
    """Le pseudo NationsGlory du membre du pays qui correspond à un de ces noms, ou None.

    Un nom complet identique gagne. Sinon, on découpe en mots ([SKH] pfpf | Empereur -> SKH, pfpf, Empereur) :
    il faut qu'UN SEUL mot corresponde à un membre, sinon c'est ambigu et on demande à la personne."""
    for name in names:
        hit = members.get(name.strip().lower())
        if hit and hit["username"].lower() not in taken:
            return hit["username"]
    for name in names:
        hits = {members[t.lower()]["username"] for t in TOKEN_RE.findall(name) if t.lower() in members}
        hits = {h for h in hits if h.lower() not in taken}
        if len(hits) == 1:
            return hits.pop()
    return None


class PseudoModal(discord.ui.Modal, title="Lier mon compte NationsGlory"):
    pseudo = discord.ui.TextInput(label="Ton pseudo NationsGlory", placeholder="ex. pfpf", min_length=2, max_length=16)

    def __init__(self, guild_id: int) -> None:
        super().__init__(timeout=600)
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("NGSync")
        guild = interaction.client.get_guild(self.guild_id)
        member = guild.get_member(interaction.user.id) if guild else None
        if cog is None or member is None:
            return await reply(interaction, "Je te trouve pas sur le serveur Discord du pays.")
        await cog.request_link(interaction, member, str(self.pseudo.value))


class AskButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ngl:ask:(?P<guild_id>\d+)"):
    """« Lier mon compte » : ouvre le formulaire du pseudo. Marche en MP et après un redémarrage du bot."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        super().__init__(discord.ui.Button(label="Lier mon compte NationsGlory", emoji="🔗",
                                           style=discord.ButtonStyle.primary, custom_id=f"ngl:ask:{guild_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PseudoModal(self.guild_id))


class UndoButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"ngl:undo:(?P<user_id>\d+):(?P<name>[A-Za-z0-9_]{2,16})"):
    """« Annuler » sous une liaison automatique, pour les officiers."""

    def __init__(self, user_id: int, name: str) -> None:
        self.user_id, self.name = user_id, name
        super().__init__(discord.ui.Button(label="Annuler la liaison", emoji="↩️", style=discord.ButtonStyle.secondary,
                                           custom_id=f"ngl:undo:{user_id}:{name}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["user_id"]), match["name"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("NGSync")
        if cog is None:
            return await reply(interaction, "La synchro NationsGlory est désactivée.")
        await cog.undo_auto_link(interaction, self.user_id, self.name)


def ask_view(guild_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(AskButton(guild_id))
    return view
