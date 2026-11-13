"""Le modèle « Bot NG » avec les plugins du catalogue : chargement, réglages, événements entre plugins, /adminbuild."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from tests.adminbuild_fakes import FakeGuild
from tests.ngbot import Member, Role, channel_mock, make_guild, ng_bot

ALL = ("adminbuild", "automod", "bienvenue", "logs", "moderation", "nationsglory", "niveaux", "tickets")


async def settle():
    """Laisse les événements (bot.dispatch) s'exécuter."""
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------- chargement
async def test_template_loads_every_catalog_plugin(tmp_path):
    from botng.bot import build_intents
    from botng.config import PAGES

    async with ng_bot(tmp_path, *ALL) as bot:
        plugins = {p.id: p for p in bot.plugin_host.plugins}
        assert set(plugins) == set(ALL) and all(p.loaded and not p.error for p in plugins.values()), \
            {p.id: p.error for p in plugins.values()}
        assert {p.section for p in PAGES} == {"bienvenue", "arrivee", "tickets", "niveaux", "automod", "logs", "moderation", "ng"}
        assert bot.intents.members and bot.intents.message_content and not bot.intents.presences
        names = {c.qualified_name for c in bot.tree.walk_commands()}
        assert {"aide", "ping", "lier", "rang", "warn", "ticket-panneau", "adminbuild", "verification-panneau"} <= names
        # commandes visibles seulement là où le plugin est activé
        assert bot.optional_module("plugins.niveaux.cog") == ("niveaux", "active")
        assert bot.optional_module("plugins.tickets.cog") == ("tickets", "active")
        assert bot.optional_module("botng.general") is None
    async with ng_bot(tmp_path / "vide") as bot:  # sans plugin : juste la base, aucun intent privilégié
        assert [p.id for p in bot.plugin_host.plugins] == [] and PAGES == []
        assert not bot.intents.members and not bot.intents.message_content
        assert {c.name for c in bot.tree.get_commands()} == {"aide", "ping", "serveur", "utilisateur", "avatar", "sondage"}
    assert build_intents([]).value == discord.Intents.default().value


async def test_broken_and_greedy_plugins_are_isolated(tmp_path):
    bad = tmp_path / "mauvais"
    (bad / "brise").mkdir(parents=True)
    (bad / "brise" / "plugin.json").write_text("{pas du json")
    (bad / "voleur").mkdir()
    (bad / "voleur" / "__init__.py").write_text("async def setup(bot):\n    pass\n")
    (bad / "voleur" / "plugin.json").write_text(json.dumps({
        "format": 1, "id": "voleur", "name": "Voleur", "version": "1.0.0", "runtime": "discord.py",
        "pages": [{"id": "x", "section": "niveaux", "title": "x", "fields": [{"key": "a", "label": "a", "type": "bool"}]}]}))
    (bad / "plante").mkdir()
    (bad / "plante" / "__init__.py").write_text("async def setup(bot):\n    raise RuntimeError('boum')\n")
    (bad / "plante" / "plugin.json").write_text(json.dumps({"format": 1, "id": "plante", "name": "Plante", "version": "1.0.0",
                                                           "runtime": "discord.py", "intents": ["presences"]}))
    async with ng_bot(tmp_path, "niveaux", extra={n: bad / n for n in ("brise", "voleur", "plante")}) as bot:
        plugins = {p.id: p for p in bot.plugin_host.plugins}
        assert "plugin.json illisible" in plugins["brise"].error
        assert "déjà prise" in plugins["voleur"].error  # un plugin peut pas prendre la section d'un autre
        assert "boum" in plugins["plante"].error and not plugins["plante"].loaded
        assert plugins["niveaux"].loaded  # les autres marchent quand même
        report = bot.plugin_host.report()
        assert report["plugins"]["plante"]["loaded"] is False and report["plugins"]["niveaux"]["loaded"] is True


# ---------------------------------------------------------------- bienvenue & accès (+ liaison NationsGlory)
async def test_welcome_and_ng_link_event(tmp_path):
    async with ng_bot(tmp_path, "bienvenue", "nationsglory") as bot:
        from plugins.bienvenue.common import fill
        guild, roles = make_guild()
        welcome_channel = channel_mock(10)
        guild.channel_map[10] = welcome_channel
        assert await bot.save_section(555, "bienvenue", {"active": True, "salon_id": 10, "depart_active": True, "mp_active": True}) == []
        assert await bot.save_section(555, "arrivee", {"role_id": 1, "role_verifie_id": 2, "retirer_quand": "liaison"}) == []
        cog = bot.get_cog("Welcome")
        m = Member(42, guild)
        assert fill("Salut {membre}, n°{nombre} sur {serveur} ({pseudo}) {x}", m) == "Salut <@42>, n°46 sur Amour (Joueur42) {x}"
        await cog.on_member_join(m)
        assert roles[1] in m.roles  # rôle d'arrivée restreint
        embed = welcome_channel.send.call_args.kwargs["embed"]
        assert embed.title == "Bienvenue sur Amour!" and "<@42>" in embed.description
        m.send.assert_awaited()  # MP de bienvenue
        bot.dispatch("ng_linked", m)  # ce que le plugin NationsGlory envoie quand un /lier est accepté
        await settle()
        assert roles[1] not in m.roles and roles[2] in m.roles
        await cog.on_member_remove(m)
        assert "Joueur42" in welcome_channel.send.call_args.kwargs["embed"].description

        await bot.save_section(555, "arrivee", {"retirer_quand": "jamais"})
        m2 = Member(43, guild)
        await cog.on_member_join(m2)
        bot.dispatch("ng_linked", m2)
        await settle()
        assert roles[1] in m2.roles  # « jamais » : le rôle reste


# ---------------------------------------------------------------- niveaux
async def test_levels(tmp_path):
    async with ng_bot(tmp_path, "niveaux") as bot:
        from plugins.niveaux.cog import level_info, progress_bar, total_xp_for, xp_for_next
        assert xp_for_next(0) == 100 and level_info(99) == (0, 99, 100) and level_info(100) == (1, 0, 155)
        assert level_info(total_xp_for(10))[0] == 10 and progress_bar(5, 10, 10) == "▰▰▰▰▰▱▱▱▱▱"
        assert await bot.save_section(555, "niveaux", {"xp_min": 50, "xp_max": 10}) == [
            "Niveaux : l'XP maximum doit être plus grand que le minimum"]

        guild, roles = make_guild()
        general = channel_mock(20)
        guild.channel_map[20] = general
        assert await bot.save_section(555, "niveaux", {"active": True, "salon_annonce_id": 20, "xp_min": 10, "xp_max": 10,
                                                       "recompenses": {"1": 3, "2": 4}, "cumuler_recompenses": False}) == []
        cog = bot.get_cog("Levels")
        author = Member(42, guild)
        guild.get_member = {42: author}.get
        msg = SimpleNamespace(author=author, guild=guild, channel=channel_mock(30))
        await cog.set_xp(555, 42, 95)
        await cog.on_message(msg)  # 95 + 10 = 105 -> niveau 1
        assert (await cog.get_xp(555, 42))["xp"] == 105
        assert general.send.call_args.args[0] == "🎉 Bravo <@42>, tu passes au **niveau 1**!"
        assert roles[3] in author.roles
        await cog.on_message(msg)  # délai de 60 s : pas d'XP
        assert (await cog.get_xp(555, 42))["xp"] == 105
        cog._last_gain.clear()
        await cog.set_xp(555, 42, 250)
        await cog.on_message(msg)  # niveau 2 : récompense remplacée (pas cumulée)
        assert roles[4] in author.roles and roles[3] not in author.roles
        assert await cog.xp_rank(555, 42) == 1
        assert (await cog.get_xp(777, 42))["xp"] == 0  # XP séparée par serveur
        assert (tmp_path / "bot" / "data" / "plugins" / "niveaux" / "niveaux.db").is_file()


# ---------------------------------------------------------------- auto-mod -> modération et logs (événements)
async def test_automod_warns_through_moderation_and_logs(tmp_path):
    async with ng_bot(tmp_path, "automod", "moderation", "logs") as bot:
        from plugins.automod.cog import domain_allowed, find_violation
        cfg = dict(bot.cfg(555, "automod"), anti_liens=True, mots_interdits=["noob", "gros mot"])
        assert find_violation("t'es un NOOB", cfg) == "mot interdit"
        assert find_violation("noobs", cfg) is None
        assert find_violation("viens sur discord.gg/abc123", cfg) == "invitation Discord"
        assert find_violation("https://nationsglory.fr/servers/jade", cfg) is None
        assert find_violation("https://site-louche.ru/x", cfg) == "lien non autorisé"
        assert domain_allowed("m.youtube.com", ["youtube.com"]) and not domain_allowed("youtube.com.evil.ru", ["youtube.com"])

        guild, _ = make_guild()
        logs = channel_mock(99, guild)
        bot.get_channel = {99: logs}.get
        await bot.save_section(555, "logs", {"salon_id": 99})
        await bot.save_section(555, "automod", {"active": True, "sanction": "avertir", "spam_messages": 3, "spam_secondes": 5})
        automod, moderation = bot.get_cog("AutoMod"), bot.get_cog("Moderation")
        author = Member(42, guild)

        def message(content, member=author):
            return SimpleNamespace(content=content, author=member, guild=guild, channel=channel_mock(30),
                                   raw_mentions=[], raw_role_mentions=[], delete=AsyncMock())

        bad = message("discord.gg/raid")
        await automod.on_message(bad)
        await settle()
        bad.delete.assert_awaited()
        warns = await moderation.get_warns(555, 42)
        assert warns[0]["reason"] == "Auto-mod : invitation Discord"  # gardé par le plugin Modération
        assert "invitation Discord" in str(logs.send.call_args.kwargs["embed"].to_dict())  # écrit par le plugin Logs

        await bot.save_section(555, "logs", {"moderation": False})  # catégorie décochée : plus rien dans les logs
        logs.send.reset_mock()
        await automod.on_message(message("discord.gg/raid2"))
        await settle()
        logs.send.assert_not_awaited()

        staff = Member(7, guild, manage=True)
        ok = message("discord.gg/partenaire", staff)
        await automod.on_message(ok)
        ok.delete.assert_not_awaited()  # le staff est exempté
        spam = [message(f"msg {i}") for i in range(3)]
        for m in spam:
            await automod.on_message(m)
        assert not spam[0].delete.await_count and spam[2].delete.await_count == 1


async def test_automod_without_moderation_plugin(tmp_path):
    async with ng_bot(tmp_path, "automod") as bot:  # sans Modération ni Logs : l'avertissement se perd, rien plante
        guild, _ = make_guild()
        await bot.save_section(555, "automod", {"active": True, "sanction": "avertir"})
        msg = SimpleNamespace(content="discord.gg/raid", author=Member(42, guild), guild=guild, channel=channel_mock(30),
                              raw_mentions=[], raw_role_mentions=[], delete=AsyncMock())
        await bot.get_cog("AutoMod").on_message(msg)
        await settle()
        msg.delete.assert_awaited()


# ---------------------------------------------------------------- tickets
async def test_tickets(tmp_path):
    async with ng_bot(tmp_path, "tickets") as bot:
        guild, roles = make_guild()
        guild.default_role = Role(0, 0)
        guild.text_channels = []
        category = discord.CategoryChannel.__new__(discord.CategoryChannel)
        transcripts = channel_mock(60)
        guild.get_channel = lambda i: category if i == 50 else {60: transcripts}.get(i)
        created = []

        async def create_text_channel(name, category, overwrites, topic, reason):
            ch = SimpleNamespace(name=name, topic=topic, mention=f"#{name}", send=AsyncMock(), delete=AsyncMock(),
                                 overwrites=overwrites, category=category)
            created.append(ch)
            guild.text_channels.append(ch)
            return ch

        guild.create_text_channel = create_text_channel
        assert await bot.save_section(555, "tickets", {"active": True, "categorie_id": 50, "role_staff_id": 5,
                                                       "salon_transcripts_id": 60}) == []
        cog = bot.get_cog("Tickets")
        member = Member(42, guild)
        guild.get_member = {42: member, 7: Member(7, guild, roles=[roles[5]])}.get

        class Response:
            def __init__(self): self.done, self.sent = False, []
            def is_done(self): return self.done
            async def defer(self, **kw): self.done = True
            async def send_message(self, content=None, **kw): self.done = True; self.sent.append(content)

        def inter(uid, channel=None):
            return SimpleNamespace(guild=guild, user=SimpleNamespace(id=uid, mention=f"<@{uid}>"), channel=channel,
                                   response=Response(), followup=SimpleNamespace(send=AsyncMock()))

        await cog.open_ticket(inter(42), "Problème de grade")
        ticket = created[0]
        assert ticket.name == "ticket-joueur42" and ticket.topic == "ticket:42 · Problème de grade"
        assert ticket.overwrites[guild.default_role].view_channel is False and ticket.overwrites[member].view_channel is True
        assert ticket.overwrites[roles[5]].manage_messages is True
        i = inter(42)
        await cog.open_ticket(i, "Encore")
        assert "T'as déjà un ticket ouvert" in i.response.sent[0] and len(created) == 1

        async def history(limit, oldest_first):
            for text in ("Salut", "J'ai perdu mon grade"):
                yield SimpleNamespace(created_at=discord.utils.utcnow(), clean_content=text, embeds=[], attachments=[],
                                      author="joueur42")

        ticket.history = history
        await cog.close_ticket(inter(7, ticket))
        assert b"J'ai perdu mon grade" in transcripts.send.call_args.kwargs["file"].fp.getvalue()
        ticket.delete.assert_awaited()


# ---------------------------------------------------------------- /adminbuild
async def test_adminbuild_configures_installed_plugins(tmp_path):
    async with ng_bot(tmp_path, "adminbuild", "bienvenue", "logs") as bot:
        from plugins.adminbuild import cog as builder
        with pytest.raises(builder.TemplateError, match="permission inconnue"):
            builder.parse("categories: [{name: X, overwrites: {Staff: {allow: [voler]}}, channels: [a]}]")
        assert builder.norm("👋・bienvenue") == builder.norm("Bienvenue") == "bienvenue"
        tpl = builder.DEFAULT_TEMPLATE
        n_channels = sum(len(c["channels"]) for c in tpl["categories"])
        guild = FakeGuild()
        plan = builder.Builder.plan(guild, tpl)
        assert len(plan["channels"]) == n_channels - 1 and plan["renamed"] == 3
        cog = bot.get_cog("Builder")
        created, binds, errors = await cog.build(guild, tpl, SimpleNamespace(user="Tristan"))
        assert errors == [], errors
        logs = next(c for c in guild.channels if c.name == "📋・logs")
        assert binds["ng.grades.MEMBER"] == 1 and binds["bienvenue.salon_id"] == 2
        errs: list[str] = []
        done = await cog.configure(guild, binds, "Amour", "jade", True, errs)
        assert set(done) == {"bienvenue", "arrivée", "tickets", "niveaux", "logs", "NationsGlory"}
        # les réglages des plugins pas installés sont gardés, et le résumé dit lesquels installer
        assert errs == ["Plugins à installer (onglet Plugins de Bot Panel) pour que ces réglages servent : "
                        "« Niveaux », « Synchro NationsGlory », « Tickets »"]
        assert bot.cfg(555, "logs")["salon_id"] == logs.id and bot.cfg(555, "bienvenue")["active"] is True
        assert bot.cfg(555, "ng")["pays"] == "Amour" and bot.cfg(555, "ng")["grades"]["MEMBER"] == 1


# ---------------------------------------------------------------- pont avec Bot Panel
async def test_panel_bridge_with_plugins(tmp_path, monkeypatch):
    from botng.panelbridge import PanelBridge, schema
    monkeypatch.setenv("NG_API_KEY", "")
    guild, _ = make_guild()
    guild.members = [Member(111, guild, manage=True), Member(222, guild)]
    async with ng_bot(tmp_path, "nationsglory", "niveaux", guilds=[guild]) as bot:
        bot.get_guild = {555: guild}.get
        s = schema()
        assert s["title"] == "Bot NG"
        fields = {f["key"]: f for p in s["pages"] for f in p["fields"]}
        assert fields["grades"]["type"] == "role_map" and [k for k, _ in fields["grades"]["keys"]] == ["LEADER", "OFFICER", "MEMBER", "RECRUIT"]
        assert fields["recompenses"]["type"] == "number_roles"
        bridge = PanelBridge(bot, bot.base)
        bot.panel_bridge = bridge
        bridge.start()
        await bridge.stop()
        report = json.loads((bridge.dir / "host.json").read_text())
        assert report["plugins"]["nationsglory"]["loaded"] and report["plugins"]["niveaux"]["version"] == "1.0.0"

        def ask(rid, page, values):
            (bridge.inbox / f"{rid}.json").write_text(json.dumps(
                {"id": rid, "scope": "555", "page": page, "values": values, "user": {"id": "111", "name": "Tristan"}}))

        ask("a1", "ng", {"active": True, "serveur": "Blue", "pays": "https://nationsglory.fr/servers/jade/countries/Amour"})
        ask("a2", "niveaux", {"xp_min": 50, "xp_max": 10})
        ask("a3", "niveaux", {"recompenses": {"5": "4242"}})
        ask("a4", "pageinconnue", {})
        for req in sorted(bridge.inbox.glob("*.json")):
            await bridge._handle(req)
        answers = {p.stem: json.loads(p.read_text()) for p in bridge.outbox.glob("*.json")}
        assert answers["a1"] == {"ok": True}
        assert bot.cfg(555, "ng")["serveur"] == "jade" and bot.cfg(555, "ng")["pays"] == "Amour"  # nettoyé par le plugin
        assert "XP maximum" in answers["a2"]["error"] and "introuvable" in answers["a3"]["error"] and not answers["a4"]["ok"]
        bot.get_cog("NGSync").set_status(555, "✅ test")
        scope = bridge._state()["scopes"][0]
        assert ["NationsGlory", "✅ test"] in scope["info"] and scope["managers"] == ["1", "111"]
        assert scope["values"]["ng"]["serveur"] == "jade"
