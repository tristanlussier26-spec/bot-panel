"""Plugin Bot Panel « Logs » (voir plugin.json et cog.py)."""
from __future__ import annotations


async def setup(bot) -> None:
    if getattr(bot, "plugin_host", None) is None:
        raise RuntimeError("ce bot supporte pas les plugins Bot Panel (bot.plugin_host manquant)")
    from .cog import setup as setup_cog  # importé ici : le bot hôte est prêt

    await setup_cog(bot)
