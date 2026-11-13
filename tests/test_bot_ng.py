"""Le modèle « Bot NG » avec les plugins du catalogue : chargement, réglages, événements entre plugins, /adminbuild."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from tests.adminbuild_fakes import FakeGuild
from tests.ngbot import Member, Role, channel_mock, make_guild, ng_bot

ALL = ("adminbuild", "automod", "bienvenue", "compteurs", "evenements", "logs", "moderation", "musique",
       "nationsglory", "niveaux", "recrutement", "roles", "tickets")


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
        assert {p.section for p in PAGES} == {"bienvenue", "arrivee", "tickets", "niveaux", "automod", "logs", "moderation", "ng",
                                              "compteurs", "recrutement", "roles", "evenements", "musique"}
        assert bot.intents.members and bot.intents.message_content and not bot.intents.presences
        names = {c.qualified_name for c in bot.tree.walk_commands()}
        assert {"aide", "ping", "lier", "rang", "warn", "ticket-panneau", "adminbuild", "verification-panneau", "compteurs-creer",
                "postuler", "recrutement-panneau", "roles-panneau", "evenement creer", "jouer", "radio", "passer"} <= names
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


# ---------------------------------------------------------------- nouveaux plugins
class Resp:
    """interaction.response simulée."""

    def __init__(self):
        self.done, self.sent, self.edits, self.modals = False, [], [], []

    def is_done(self): return self.done
    async def defer(self, **kw): self.done = True
    async def send_message(self, content=None, **kw): self.done = True; self.sent.append(content if content is not None else kw)
    async def edit_message(self, **kw): self.done = True; self.edits.append(kw)
    async def send_modal(self, modal): self.done = True; self.modals.append(modal)


def inter(guild, user, channel=None, message=None):
    return SimpleNamespace(guild=guild, guild_id=guild.id, user=user, channel=channel, channel_id=getattr(channel, "id", 0),
                           response=Resp(), followup=SimpleNamespace(send=AsyncMock()), message=message)


def said(i) -> str:
    """Ce que le bot a répondu (réponse directe ou suivi)."""
    if i.response.sent:
        last = i.response.sent[-1]
        return last if isinstance(last, str) else str(last)
    return str(i.followup.send.call_args)


async def test_counters(tmp_path):
    import time as _time
    async with ng_bot(tmp_path, "compteurs", "nationsglory") as bot:
        cog = bot.get_cog("Counters")
        guild, _ = make_guild()
        guild.members = [Member(i, guild) for i in range(1, 5)] + [Member(900, guild, bot=True)]
        guild.member_count, guild.premium_subscription_count = 5, 2

        class VoiceChannel(SimpleNamespace):
            async def edit(self, name, reason=None):
                self.name = name
                self.edits += 1

        chans = {i: VoiceChannel(id=i, name="vieux nom", edits=0) for i in (71, 72, 73)}
        guild.get_channel = chans.get
        assert await bot.save_section(555, "compteurs", {"active": True, "salon_membres": 71, "salon_humains": 72,
                                                         "salon_pays": 73, "format_pays": "🏳️ {pays} : {n}"}) == []
        assert chans[71].name == "vieux nom"  # bot.get_guild connaît pas ce faux serveur : rien encore
        assert await cog.refresh(guild, force=True) == 2
        assert chans[71].name == "👥 Membres : 5" and chans[72].name == "🧑 Humains : 4"
        assert chans[73].name == "vieux nom"  # pas encore de nombre du pays
        bot.dispatch("ng_country_update", 555, "Amour", {f"j{i}": {} for i in range(23)})  # du plugin NationsGlory
        await settle()
        guild.member_count = 6
        guild.members.append(Member(5, guild))
        assert await cog.refresh(guild) == 1  # le pays (jamais renommé) ; les autres attendent 5 min (limite de Discord)
        assert chans[73].name == "🏳️ Amour : 23" and chans[71].name == "👥 Membres : 5" and 555 in cog.pending
        cog.last_edit = {k: _time.monotonic() - 301 for k in cog.last_edit}
        assert await cog.refresh(guild) == 2 and chans[71].name == "👥 Membres : 6"
        assert await cog.refresh(guild, force=True) == 0  # rien n'a changé : aucun appel à Discord

        # /compteurs-creer : catégorie verrouillée en haut, salons créés et réglés
        created = []

        class Category(SimpleNamespace):
            async def create_voice_channel(self, name, reason=None):
                ch = VoiceChannel(id=100 + len(created), name=name, edits=0)
                created.append(ch)
                chans[ch.id] = ch
                return ch

        guild.default_role = Role(0, 0)
        guild.create_category = AsyncMock(return_value=Category(name="📊 Statistiques"))
        i = inter(guild, Member(1, guild, manage=True))
        await cog.creer.callback(cog, i)
        assert [c.name for c in created] == ["👥 Membres : 6", "🧑 Humains : 5", "🚀 Boosts : 2", "🏳️ Amour : 23"]
        assert guild.create_category.call_args.kwargs["overwrites"][guild.default_role].connect is False
        assert bot.cfg(555, "compteurs")["salon_boosts"] == 102 and "4 salon(s)" in said(i)


async def test_recruitment(tmp_path):
    async with ng_bot(tmp_path, "recrutement", "logs") as bot:
        cog = bot.get_cog("Recruitment")
        guild, roles = make_guild()
        staff_channel = channel_mock(80)
        guild.channel_map[80] = staff_channel
        candidate = Member(42, guild, roles=[roles[1]])
        officer = Member(7, guild, roles=[roles[5]])
        rando = Member(8, guild)
        guild.get_member = {42: candidate, 7: officer, 8: rando}.get

        i = inter(guild, candidate)
        await cog.start_application(i)
        assert "fermé" in said(i)  # pas encore activé
        assert await bot.save_section(555, "recrutement", {
            "active": True, "salon_candidatures_id": 80, "role_staff_id": 5, "roles_acceptes": [3, 4], "role_retire_id": 1,
            "questions": ["Ton pseudo NationsGlory", "Pourquoi nous?"], "attente_jours": 7}) == []
        i = inter(guild, candidate)
        await cog.start_application(i)
        modal = i.response.modals[0]
        assert [c.text for c in modal.children] == ["Ton pseudo NationsGlory", "Pourquoi nous?"]

        i = inter(guild, candidate)
        await cog.submit(i, [("Ton pseudo NationsGlory", "Tristan_QC"), ("Pourquoi nous?", "Parce que")])
        sent = staff_channel.send.call_args.kwargs
        assert sent["content"] == "<@&5>" and [c.custom_id for c in sent["view"].children] == ["rec:ok:1", "rec:no:1"]
        assert sent["embed"].fields[0].value == "Tristan_QC"
        i = inter(guild, candidate)
        await cog.start_application(i)
        assert "déjà en attente" in said(i)

        message = SimpleNamespace(embeds=[sent["embed"]], edit=AsyncMock())
        i = inter(guild, rando, message=message)
        await cog.decide(i, 1, accept=True)
        assert "pas le droit" in said(i)
        i = inter(guild, officer, message=message)
        await cog.decide(i, 1, accept=True)
        assert {r.id for r in candidate.roles} == {3, 4}  # rôles donnés, « Nouveau » retiré
        assert "acceptée" in candidate.send.call_args.args[0] and i.response.edits[0]["view"] is None
        i = inter(guild, officer, message=message)
        await cog.decide(i, 1, accept=True)
        assert "déjà été traitée" in said(i)

        # refus avec raison, puis délai avant de repostuler
        other = Member(43, guild)
        guild.get_member = {42: candidate, 7: officer, 43: other}.get
        await cog.submit(inter(guild, other), [("Ton pseudo NationsGlory", "Bob")])
        i = inter(guild, officer, message=SimpleNamespace(embeds=[staff_channel.send.call_args.kwargs["embed"]], edit=AsyncMock()))
        await cog.decide(i, 2, accept=False, reason="Trop jeune")
        assert "refusée" in other.send.call_args.args[0] and "Trop jeune" in other.send.call_args.args[0]
        i = inter(guild, other)
        await cog.start_application(i)
        assert "repostuler" in said(i)


async def test_button_roles(tmp_path):
    async with ng_bot(tmp_path, "roles") as bot:
        cog = bot.get_cog("ButtonRoles")
        guild, roles = make_guild()
        member = Member(42, guild)
        assert await bot.save_section(555, "roles", {"roles_1": [3, 4], "roles_3": [1, 2], "unique_3": True}) == []
        channel = channel_mock(30)
        i = inter(guild, Member(7, guild, manage=True), channel=channel)
        await cog.panneau.callback(cog, i, None)
        view = channel.send.call_args.kwargs["view"]
        assert [(c.custom_id, c.item.label) for c in view.children] == [("rb:1:3", "Niveau 1"), ("rb:1:4", "Niveau 2")]

        i = inter(guild, member)
        await cog.toggle(i, "1", 3)
        assert roles[3] in member.roles and "ajouté" in said(i)
        await cog.toggle(inter(guild, member), "1", 4)
        assert roles[3] in member.roles and roles[4] in member.roles  # plusieurs permis
        i = inter(guild, member)
        await cog.toggle(i, "1", 3)
        assert roles[3] not in member.roles and "enlevé" in said(i)
        i = inter(guild, member)
        await cog.toggle(i, "1", 5)  # « Staff » est pas offert : refusé
        assert roles[5] not in member.roles and "plus offert" in said(i)

        await cog.toggle(inter(guild, member), "3", 1)
        i = inter(guild, member)
        await cog.toggle(i, "3", 2)  # un seul à la fois : 1 est remplacé par 2
        assert roles[2] in member.roles and roles[1] not in member.roles and "au lieu de" in said(i)


async def test_events(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    async with ng_bot(tmp_path, "evenements") as bot:
        from plugins.evenements.cog import parse_when, reminders
        tz = ZoneInfo("America/Toronto")
        now = datetime(2026, 11, 12, 18, 0, tzinfo=tz)
        assert parse_when("25/11 20:30", tz, now) == datetime(2026, 11, 25, 20, 30, tzinfo=tz)
        assert parse_when("25/11/2027 20h", tz, now) == datetime(2027, 11, 25, 20, 0, tzinfo=tz)
        assert parse_when("demain 21h", tz, now) == datetime(2026, 11, 13, 21, 0, tzinfo=tz)
        assert parse_when("20h30", tz, now) == datetime(2026, 11, 12, 20, 30, tzinfo=tz)
        assert parse_when("9h", tz, now) == datetime(2026, 11, 13, 9, 0, tzinfo=tz)  # passé aujourd'hui : demain
        assert parse_when("05/01 10:00", datetime(2026, 12, 20, tzinfo=tz).tzinfo, datetime(2026, 12, 20, tzinfo=tz)).year == 2027
        assert parse_when("31/02 10:00", tz, now) is None and parse_when("bientôt", tz, now) is None
        assert reminders({"rappels": ["10", "60", "x", "0", "99999"]}) == [60, 10, 0]

        cog = bot.get_cog("Events")
        guild, roles = make_guild()
        guild.channel_map[90] = channel = channel_mock(90)
        channel.send = AsyncMock(return_value=SimpleNamespace(id=5000))
        channel.get_partial_message = lambda mid: SimpleNamespace(edit=AsyncMock())
        organizer, member = Member(7, guild, roles=[roles[5]]), Member(42, guild)
        i = inter(guild, organizer, channel=channel)
        kind = SimpleNamespace(value="guerre", name="⚔️ Guerre")
        await cog.creer.callback(cog, i, "Assaut", "demain 21h", kind)
        assert "désactivés" in said(i)
        assert await bot.save_section(555, "evenements", {"active": True, "salon_id": 90, "role_alerte_id": 4,
                                                          "role_organisateur_id": 5, "rappels": ["60", "10"]}) == []
        i = inter(guild, member, channel=channel)
        await cog.creer.callback(cog, i, "Assaut", "demain 21h", kind)
        assert "Faut le rôle" in said(i)
        i = inter(guild, organizer, channel=channel)
        await cog.creer.callback(cog, i, "Assaut", "demain 21h", kind, "Rendez-vous au bunker")
        assert "Guerre n°1 planifié" in said(i)
        post = channel.send.call_args.kwargs
        assert post["content"] == "<@&4>" and post["embed"].title == "⚔️ Assaut"
        assert [c.custom_id for c in post["view"].children] == ["evt:oui:1", "evt:peut:1", "evt:non:1"]

        i = inter(guild, member)
        await cog.answer(i, 1, "oui")
        fields = {f.name: f.value for f in i.response.edits[0]["embed"].fields}
        assert fields["✅ Présents (1)"] == "<@42>" and fields["❌ Absents (0)"] == "—"
        await cog.answer(inter(guild, member), 1, "non")  # change d'idée
        assert (await cog.answers(1))["oui"] == [] and (await cog.answers(1))["non"] == [42]
        await cog.answer(inter(guild, organizer), 1, "oui")

        start = (await cog.get(555, 1))["start_ts"]
        assert await cog.due_reminders(now=start - 3700) == []
        assert [m for _, m in await cog.due_reminders(now=start - 3500)] == [60]
        assert await cog.due_reminders(now=start - 3400) == []  # déjà envoyé
        assert [m for _, m in await cog.due_reminders(now=start + 30)] == [0]  # bot arrêté : juste le plus proche
        bot.get_guild = {555: guild}.get
        channel.send.reset_mock()
        await cog.send_reminder(await cog.get(555, 1), 10)
        text = channel.send.call_args.args[0]
        assert text.startswith("<@&4> ⏰ ⚔️ **Assaut** commence") and "Présents : <@7>" in text

        i = inter(guild, member)
        await cog.annuler.callback(cog, i, 1)
        assert "pas le droit" in said(i)
        await cog.annuler.callback(cog, inter(guild, organizer), 1)
        i = inter(guild, member)
        await cog.answer(i, 1, "oui")
        assert "annulé" in said(i)


async def test_music(tmp_path, monkeypatch):
    async with ng_bot(tmp_path, "musique") as bot:
        from plugins.musique.cog import Track, parse_radios, site_of, title_from_url
        cog = bot.get_cog("Music")
        assert site_of("www.youtube.com") == "YouTube" and site_of("open.spotify.com") == "Spotify" and site_of("radio.ca") is None
        assert title_from_url("https://x.com/dossier/Ma%20chanson.mp3?a=1") == "Ma chanson.mp3"
        assert parse_radios(["Radio Amour | https://r.example/flux", "brisé", "Rien | ftp://x"]) == {"Radio Amour": "https://r.example/flux"}

        addresses = {"radio.example": ["93.184.216.34"], "interne.example": ["192.168.2.1"]}

        async def resolve(host):
            return addresses[host]

        monkeypatch.setattr(cog, "resolve", resolve)
        assert await cog.check_url("https://radio.example/flux.mp3") is None
        assert "réseau privé" in await cog.check_url("http://interne.example/x.mp3")
        assert "YouTube est pas supporté" in await cog.check_url("https://youtu.be/abc")
        assert "http" in await cog.check_url("file:///etc/passwd")

        # Lecteur avec un faux salon vocal : file, enchaînement, passer, départ quand plus personne écoute
        class Voice:
            def __init__(self):
                self.after, self.playing, self.paused, self.connected = None, False, False, True
                self.channel = SimpleNamespace(members=[Member(42, None)])
                self.played = []

            def is_connected(self): return self.connected
            def is_playing(self): return self.playing
            def is_paused(self): return self.paused
            def play(self, source, after): self.played.append(source); self.playing, self.after = True, after
            def stop(self): self.playing = False; self.after(None)
            async def disconnect(self, force=False): self.connected = False

        monkeypatch.setattr(cog, "make_source", lambda track, volume: f"source:{track.title}@{volume}")
        await bot.save_section(555, "musique", {"active": True, "volume": 80, "quitter_minutes": 1})
        player = cog.player(555)
        player.voice = voice = Voice()
        assert await cog.enqueue(player, Track("A", "https://radio.example/a.mp3", 42)) == "▶️ Je joue **A**."
        assert "position 1" in await cog.enqueue(player, Track("B", "https://radio.example/b.mp3", 42))
        assert voice.played == ["source:A@0.8"] and player.current.title == "A"
        voice.stop()  # fin de A (ou /passer) : B part tout seul
        await settle()
        assert voice.played[-1] == "source:B@0.8" and player.current.title == "B" and not player.queue
        voice.stop()
        await settle()
        assert player.current is None
        player.idle_since -= 61  # plus de musique depuis 1 minute : il quitte
        await cog.idle_check.coro(cog)
        assert not voice.connected and player.voice is None
