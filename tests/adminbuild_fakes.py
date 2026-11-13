"""Faux serveur Discord pour /adminbuild (catégories, salons et rôles créés en mémoire)."""
import itertools
from types import SimpleNamespace

import discord

ids = itertools.count(10_000)


class FakeRole(SimpleNamespace):
    __hash__ = object.__hash__  # comme les vrais rôles, utilisables comme clés des permissions


class FakeChannel(SimpleNamespace):
    async def edit(self, reason=None, name=None, category=None, sync_permissions=False):
        if name is not None:
            self.name = name.lower().replace(" ", "-") if self.type == discord.ChannelType.text else name
        if category is not None:
            self.category_id = category.id
            self.synced = sync_permissions


class FakeCategory:
    def __init__(self, guild, name, overwrites=None):
        self.id, self.name, self.overwrites, self.guild = next(ids), name, overwrites or {}, guild

    @property
    def channels(self):
        return [c for c in self.guild.channels if c.category_id == self.id]

    async def edit(self, name, reason=None):
        self.name = name

    async def _make(self, name, kind, overwrites=None, **kw):
        ch = FakeChannel(id=next(ids), name=name.lower().replace(" ", "-") if kind == discord.ChannelType.text else name,
                         type=kind, overwrites=overwrites, category_id=self.id, **kw)
        self.guild.channels.append(ch)
        self.guild.created_channels.append(ch)
        return ch

    async def create_text_channel(self, name, overwrites=None, topic=None, slowmode_delay=0, reason=None):
        return await self._make(name, discord.ChannelType.text, overwrites, topic=topic)

    async def create_voice_channel(self, name, overwrites=None, reason=None):
        return await self._make(name, discord.ChannelType.voice, overwrites)


class FakeGuild:
    """Un serveur comme celui de Tristan après un premier /adminbuild sans emojis."""

    def __init__(self):
        self.id = 555
        self.default_role = FakeRole(id=555, name="@everyone")
        self.roles = [self.default_role, FakeRole(id=1, name="Membre")]  # « Membre » existe déjà
        self.channels, self.created_roles, self.created_channels = [], [], []
        accueil, discussion = FakeCategory(self, "Accueil"), FakeCategory(self, "Discussion")
        self.categories = [accueil, discussion]
        self.channels += [
            FakeChannel(id=2, name="bienvenue", type=discord.ChannelType.text, category_id=accueil.id),
            FakeChannel(id=3, name="Vocal", type=discord.ChannelType.voice, category_id=discussion.id),
        ]
        self.me = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True))

    async def create_role(self, name, permissions, colour, hoist, mentionable, reason):
        role = FakeRole(id=next(ids), name=name, permissions=permissions)
        self.roles.append(role)
        self.created_roles.append(role)
        return role

    async def create_category(self, name, overwrites, reason):
        cat = FakeCategory(self, name, overwrites)
        self.categories.append(cat)
        return cat


