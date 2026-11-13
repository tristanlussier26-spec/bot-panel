"""Un vrai « Bot NG » (modèle templates/bot-ng) avec des plugins du catalogue, sans Discord, pour les tests."""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import discord

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "plugins"
for path in (ROOT / "templates" / "bot-ng", ROOT):  # botng, et « plugins.<id> » depuis le catalogue
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@asynccontextmanager
async def ng_bot(tmp: Path, *plugin_ids: str, guilds=(), extra: dict[str, Path] | None = None):
    """Bot NG dont le dossier plugins/ contient les plugins demandés (liens vers le catalogue)."""
    from botng.bot import Bot
    from botng.plugins import register_pages

    base = tmp / "bot"
    (base / "plugins").mkdir(parents=True)
    for pid in plugin_ids:
        (base / "plugins" / pid).symlink_to(CATALOG / pid, target_is_directory=True)
    for name, folder in (extra or {}).items():
        (base / "plugins" / name).symlink_to(folder, target_is_directory=True)
    sys.path.append(str(base))  # les plugins qui sont pas dans le catalogue (ex. un plugin brisé de test)
    with patch.object(Bot, "guilds", new_callable=PropertyMock, return_value=list(guilds)):
        bot = Bot(base)
        bot.tree.sync = AsyncMock(return_value=[])
        await bot._async_setup_hook()  # ce que login() fait avant setup_hook (boucle d'événements, bot.dispatch)
        bot._connection.user = SimpleNamespace(id=999, name="Bot NG")
        await bot.setup_hook()
        if (ng := bot.get_cog("NGSync")) is not None:
            ng.tick.cancel()
        try:
            yield bot
        finally:
            for ext in list(bot.extensions):
                await bot.unload_extension(ext)
            await bot.plugin_host.close_dbs()
            await bot.db.close()
            register_pages([])
            sys.path.remove(str(base))


# ---- Faux Discord -----------------------------------------------------------------------------------
class Role:
    def __init__(self, rid, position, name=None, managed=False):
        self.id, self.position, self.name, self.managed = rid, position, name or f"r{rid}", managed
        self.mention = f"<@&{rid}>"
        self.color = discord.Color.default()

    def __lt__(self, o): return self.position < o.position
    def __ge__(self, o): return self.position >= o.position
    def __eq__(self, o): return isinstance(o, Role) and o.id == self.id
    def __hash__(self): return self.id
    def is_default(self): return self.id == 0


class Member:
    def __init__(self, uid, guild, roles=(), manage=False, bot=False):
        self.id, self.guild, self.roles, self.bot = uid, guild, list(roles), bot
        self.mention, self.display_name, self.name = f"<@{uid}>", f"Joueur{uid}", f"joueur{uid}"
        self.display_avatar = SimpleNamespace(url="https://x/a.png")
        self.guild_permissions = SimpleNamespace(manage_messages=manage, manage_channels=manage, manage_guild=manage,
                                                 administrator=False, manage_roles=manage)
        self.created_at = discord.utils.utcnow()
        self.add_roles = AsyncMock(side_effect=lambda *r, reason=None: self.roles.extend(r))
        self.remove_roles = AsyncMock(side_effect=lambda *r, reason=None: [self.roles.remove(x) for x in r])
        self.send = AsyncMock()
        self.timeout = AsyncMock()
        self.edit = AsyncMock()

    def __str__(self): return self.name
    def __hash__(self): return self.id
    def __eq__(self, o): return isinstance(o, Member) and o.id == self.id

    @property
    def top_role(self):
        return max(self.roles, key=lambda r: r.position, default=Role(0, 0))


def make_guild(gid=555, name="Amour"):
    roles = {i: Role(i, i, name) for i, name in ((1, "Nouveau"), (2, "Vérifié"), (3, "Niveau 1"), (4, "Niveau 2"), (5, "Staff"))}
    guild = SimpleNamespace(id=gid, name=name, owner_id=1, member_count=46, get_role=roles.get, icon=None,
                            channels=[], roles=list(roles.values()), members=[])
    guild.channel_map = {}
    guild.get_channel = guild.channel_map.get
    guild.me = Member(999, guild, roles=[Role(99, 99, "Bot")])
    guild.get_member = lambda i: None
    return guild, roles


def channel_mock(cid=10, guild=None):
    return SimpleNamespace(id=cid, mention=f"<#{cid}>", send=AsyncMock(), category_id=None,
                           guild=guild or SimpleNamespace(id=555))
