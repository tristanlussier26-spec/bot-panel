"""Ximbo 1.1 : /ximbo on depuis le clavardage d'un vocal, carte du vocal (demande de contrôle, vote pour passer),
contrôleurs invités, ajout avec « mélanger » et « jouer ensuite », suggestions et playlists perso."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.ngbot import Member, ng_bot

wavelink = pytest.importorskip("wavelink")


def track(n: int, *, stream: bool = False) -> "wavelink.Playable":
    return wavelink.Playable({
        "encoded": f"QAAA{n}", "pluginInfo": {}, "userData": {},
        "info": {"identifier": f"id{n}", "isSeekable": True, "author": f"Artiste {n}", "length": 200_000 + n * 1000,
                 "isStream": stream, "position": 0, "title": f"Chanson {n}", "uri": f"https://youtu.be/id{n}",
                 "artworkUrl": None, "isrc": None, "sourceName": "youtube"}})


class FakePlayer:
    """Ce que le cog utilise d'un wavelink.Player."""

    def __init__(self, channel, room_id):
        self.channel, self.room_id = channel, room_id
        self.queue = wavelink.Queue()
        self.current, self.paused, self.playing, self.volume = None, False, False, 60
        self.autoplay = wavelink.AutoPlayMode.partial
        self.filter_name = "Aucun"

        async def play(t, volume=None):
            self.current, self.playing = t, True
        self.play = AsyncMock(side_effect=play)
        self.skip = AsyncMock()
        self.disconnect = AsyncMock()


class Channel(SimpleNamespace):
    """Salon texte ou vocal : envoie des messages, garde ses permissions."""

    def __init__(self, cid, members=()):
        super().__init__(id=cid, mention=f"<#{cid}>", name=f"salon{cid}", members=list(members), perms={})
        self.sent = []

        async def send(*args, **kwargs):
            msg = SimpleNamespace(id=cid * 100 + len(self.sent), args=args, kwargs=kwargs)
            self.sent.append(msg)
            return msg
        self.send = AsyncMock(side_effect=send)

        async def set_permissions(target, overwrite=None, reason=None, **perms):
            self.perms[target.id] = perms or None
        self.set_permissions = AsyncMock(side_effect=set_permissions)
        self.partial = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        self.get_partial_message = lambda mid: self.partial


def make_guild(channels):
    room = Channel(77)
    guild = SimpleNamespace(id=1, default_role="everyone", me="moi", voice_client=None,
                            get_channel=lambda cid: channels.get(cid),
                            get_member=lambda uid: None,
                            create_category=AsyncMock(return_value=SimpleNamespace(id=66)),
                            create_text_channel=AsyncMock(side_effect=lambda *a, **k: channels.setdefault(77, room)))
    return guild


def interaction(guild, user, channel_id=500, message_id=None):
    response = SimpleNamespace(defer=AsyncMock(), is_done=lambda: False, send_message=AsyncMock(),
                               edit_message=AsyncMock(), send_modal=AsyncMock())

    async def defer(**kw):
        response.is_done = lambda: True
    response.defer.side_effect = defer
    return SimpleNamespace(guild=guild, guild_id=1, user=user, channel_id=channel_id,
                           channel=guild.get_channel(channel_id) or SimpleNamespace(id=channel_id),
                           message=SimpleNamespace(id=message_id), response=response,
                           followup=SimpleNamespace(send=AsyncMock()), client=None)


def said(i) -> str:
    """Ce qu'on a répondu au membre (réponse directe ou après « defer »)."""
    for call in (i.followup.send, i.response.send_message):
        if call.await_args:
            return call.await_args.kwargs.get("content") or ""
    return ""


async def test_commands_and_tables(tmp_path):
    async with ng_bot(tmp_path, "ximbo") as bot:
        names = {c.qualified_name for c in bot.tree.walk_commands()}
        assert {"ximbo on", "ximbo off", "ximbo inviter", "ximbo retirer", "ximbo jouer", "ximbo file",
                "ximbo playlist sauver", "ximbo playlist charger", "ximbo playlist liste",
                "ximbo playlist supprimer", "ximbo-panneau", "ximbo-fermer", "ximbo-statut"} <= names
        cog = bot.get_cog("Ximbo")
        cols = {r["name"] for r in await cog.db.all("PRAGMA table_info(rooms)")}
        assert {"voice_id", "card_id", "card_channel"} <= cols
        from plugins.ximbo.cog import ROOM_COLUMNS  # déjà là : un 2e ALTER échoue, et le chargement l'ignore
        for column in ROOM_COLUMNS:
            with pytest.raises(Exception):
                await cog.db.run(f"ALTER TABLE rooms ADD COLUMN {column}")
        assert cog.votes_needed([1]) == 1 and cog.votes_needed([1, 2, 3]) == 2 and cog.votes_needed([1, 2, 3, 4]) == 2


async def test_old_database_gets_new_columns(tmp_path):
    """Une base de la 1.0.0 (sans voice_id, card_id, card_channel) est mise à jour au chargement."""
    import sqlite3

    folder = tmp_path / "bot" / "data" / "plugins" / "ximbo"
    folder.mkdir(parents=True)
    with sqlite3.connect(folder / "ximbo.db") as old:
        old.executescript("CREATE TABLE rooms (channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, owner_id INTEGER "
                          "NOT NULL, message_id INTEGER, volume INTEGER NOT NULL DEFAULT 60, created_at TEXT);"
                          "INSERT INTO rooms VALUES (10, 1, 5, 11, 60, NULL);")
    async with ng_bot(tmp_path, "ximbo") as bot:
        cog = bot.get_cog("Ximbo")
        if not (cog.host.data_dir("ximbo") / "ximbo.db").samefile(folder / "ximbo.db"):
            pytest.skip("la base du plugin est rangée ailleurs")
        room = await cog.room(10)
        assert room["owner_id"] == 5 and room["card_id"] is None and cog.rooms == {10: 1}


async def test_ximbo_on_from_voice_chat(tmp_path):
    async with ng_bot(tmp_path, "ximbo") as bot:
        cog = bot.get_cog("Ximbo")
        voice = Channel(500)
        channels = {500: voice}
        guild = make_guild(channels)
        owner = Member(5, guild)
        owner.display_name = "Lequeb"

        # Pas activé
        i = interaction(guild, owner)
        await cog.x_on.callback(cog, i)
        assert "pas activé" in said(i)
        assert await bot.save_section(1, "ximbo", {"active": True}) == []

        # Pas dans un vocal
        owner.voice = None
        i = interaction(guild, owner)
        await cog.x_on.callback(cog, i)
        assert "Rejoins d'abord" in said(i)

        # Dans le vocal, Lavalink pas encore prêt : le salon s'ouvre quand même et la carte est publiée dans le vocal
        owner.voice = SimpleNamespace(channel=voice)
        voice.members = [owner]
        i = interaction(guild, owner)
        await cog.x_on.callback(cog, i)
        text = said(i)
        assert "démarre encore" in text and "<#77>" in text and "/ximbo inviter" in text
        room = await cog.room(77)
        assert room["owner_id"] == 5 and room["voice_id"] == 500
        card = voice.sent[-1]
        assert card.kwargs["embed"].title == "🎧 Ximbo est prêt" and card.kwargs["view"] is not None
        assert (room["card_id"], room["card_channel"]) == (card.id, 500)

        # Deuxième /ximbo on : le même salon est réutilisé
        i = interaction(guild, owner)
        await cog.x_on.callback(cog, i)
        assert guild.create_text_channel.await_count == 1 and "<#77>" in said(i)


async def test_controllers_request_and_vote(tmp_path):
    async with ng_bot(tmp_path, "ximbo") as bot:
        cog = bot.get_cog("Ximbo")
        voice = Channel(500)
        room_channel = Channel(77)
        channels = {500: voice, 77: room_channel}
        guild = make_guild(channels)
        owner, ami, autre = Member(5, guild), Member(6, guild), Member(7, guild)
        members = {5: owner, 6: ami, 7: autre}
        guild.get_member = members.get
        await cog.db.run("INSERT INTO rooms (channel_id, guild_id, owner_id, message_id, volume, card_id, card_channel) "
                         "VALUES (77, 1, 5, 88, 60, 555, 500)")
        cog.rooms[77] = 1
        room = await cog.room(77)

        assert await cog.is_controller(owner, room) and not await cog.is_controller(ami, room)
        await cog.add_guest(room_channel, ami)
        assert await cog.is_controller(ami, room) and room_channel.perms[6]["view_channel"]
        assert await cog.controllers(room) == [5, 6]
        await cog.remove_guest(room_channel, ami)
        assert not await cog.is_controller(ami, room) and await cog.controllers(room) == [5]

        # 🙋 Demander le contrôle depuis la carte du vocal
        i = interaction(guild, ami, channel_id=500, message_id=555)
        await cog.card_request(i)
        assert "Demande envoyée" in said(i)
        ask = room_channel.sent[-1]
        assert "demande à contrôler" in ask.args[0] and "<@5>" in ask.args[0]
        i = interaction(guild, ami, channel_id=500, message_id=555)
        await cog.card_request(i)
        assert "déjà envoyée" in said(i)  # une demande à la fois

        # Quelqu'un d'autre que le propriétaire peut pas accepter ; le propriétaire, oui
        view = ask.kwargs["view"]
        i = interaction(guild, autre, channel_id=77)
        i.channel, i.client = room_channel, bot
        await view._decide(i, True)
        assert "Seul le propriétaire" in said(i) and not await cog.is_controller(ami, room)
        i = interaction(guild, owner, channel_id=77)
        i.channel, i.client = room_channel, bot
        await view._decide(i, True)
        assert "peut maintenant contrôler" in i.response.edit_message.await_args.kwargs["content"]
        assert await cog.is_controller(ami, room)

        # Carte terminée ou inconnue
        i = interaction(guild, autre, channel_id=500, message_id=999)
        await cog.card_request(i)
        assert "terminée" in said(i)

        # ⏭️ Vote : 3 personnes dans le vocal, il faut 2 votes ; un contrôleur passe tout de suite
        player = FakePlayer(voice, 77)
        player.current = track(1)
        voice.members = [owner, ami, autre, Member(8, guild)]
        cog.player_for = lambda g, rid: player if rid == 77 else None
        for m in (owner, ami, autre):
            m.voice = SimpleNamespace(channel=voice)
        voice.members = [owner, autre, Member(9, guild)]
        i = interaction(guild, autre, channel_id=500, message_id=555)
        await cog.card_vote(i)
        assert "1 / 2" in said(i) and player.skip.await_count == 0
        i = interaction(guild, autre, channel_id=500, message_id=555)
        await cog.card_vote(i)
        assert "Vote retiré" in said(i)
        i = interaction(guild, autre, channel_id=500, message_id=555)
        await cog.card_vote(i)
        nine = voice.members[2]
        nine.voice = SimpleNamespace(channel=voice)
        i = interaction(guild, nine, channel_id=500, message_id=555)
        await cog.card_vote(i)
        assert "Majorité" in said(i) and player.skip.await_count == 1
        i = interaction(guild, ami, channel_id=500, message_id=555)
        await cog.card_vote(i)
        assert "tu contrôles le bot" in said(i) and player.skip.await_count == 2

        # Pas dans le vocal : pas de vote
        dehors = Member(10, guild)
        dehors.voice = None
        i = interaction(guild, dehors, channel_id=500, message_id=555)
        await cog.card_vote(i)
        assert "Viens dans" in said(i)

        # La carte montre le titre, les contrôleurs et finit « arrêtée »
        from plugins.ximbo.cog import card_embed
        e = card_embed(room, player, [5, 6], votes=1, needed=2)
        assert e.title == "Chanson 1" and "<@5> <@6>" in [f.value for f in e.fields if f.name == "Contrôlé par"][0]
        assert any(f.value == "⏭️ 1 / 2" for f in e.fields)
        assert card_embed(room, None, [], ended=True).title == "⏹️ Ximbo s'est arrêté"
        await cog.end_session(guild, 77, player)
        player.disconnect.assert_awaited()
        assert voice.partial.edit.await_args.kwargs["view"] is None


async def test_enqueue_options_and_playlists(tmp_path):
    async with ng_bot(tmp_path, "ximbo") as bot:
        cog = bot.get_cog("Ximbo")
        voice = Channel(500)
        room_channel = Channel(77)
        guild = make_guild({500: voice, 77: room_channel})
        owner = Member(5, guild)
        owner.voice = SimpleNamespace(channel=voice)
        await cog.db.run("INSERT INTO rooms (channel_id, guild_id, owner_id, message_id, volume) VALUES (77, 1, 5, 88, 60)")
        cog.rooms[77] = 1
        room = await cog.room(77)
        player = FakePlayer(voice, 77)
        cog.node_status = "prêt"
        cog.ensure_player = AsyncMock(return_value=player)
        cog.refresh = AsyncMock()
        cog.refresh_card = AsyncMock()

        # Un titre : il joue tout de suite
        cog.search = AsyncMock(return_value=[track(1)])
        msg = await cog.enqueue(owner, room, "chanson 1")
        assert msg.startswith("➕ **Chanson 1**") and player.current.title == "Chanson 1"
        assert player.current.extras.requester == 5

        # Une playlist (lien) : tout est ajouté à la fin
        pl = SimpleNamespace(name="Mon mix", tracks=[track(2), track(3), track(4)])
        cog.search = AsyncMock(return_value=wavelink.Playlist.__new__(wavelink.Playlist))
        cog.search.return_value.__dict__.update(name="Mon mix", tracks=pl.tracks)
        msg = await cog.enqueue(owner, room, "https://open.spotify.com/playlist/x")
        assert "Mon mix" in msg and "3" in msg and [t.title for t in player.queue] == ["Chanson 2", "Chanson 3", "Chanson 4"]

        # « Jouer ensuite » : en tête de file, dans l'ordre
        cog.search = AsyncMock(return_value=[track(5)])
        msg = await cog.enqueue(owner, room, "chanson 5", play_next=True)
        assert "joue ensuite" in msg and player.queue[0].title == "Chanson 5"
        await cog.enqueue(owner, room, tracks=[track(6), track(7)], label="💾 **X**", play_next=True)
        assert [t.title for t in list(player.queue)[:3]] == ["Chanson 6", "Chanson 7", "Chanson 5"]

        # Mélanger : les mêmes titres, ajoutés à la fin
        many = [track(n) for n in range(10, 30)]
        msg = await cog.enqueue(owner, room, tracks=many, label="💾 **Gros**", shuffle=True)
        assert "🔀 mélangée" in msg and "20" in msg
        added = [t.title for t in list(player.queue)[-20:]]
        assert sorted(added) == sorted(t.title for t in many)

        # Pas encore prêt : message clair
        cog.node_status = "en attente"
        with pytest.raises(Exception, match="démarre encore"):
            await cog.enqueue(owner, room, "x")
        cog.node_status = "prêt"

        # ---- playlists perso
        import plugins.ximbo.cog as mod
        real_player = mod.XimboPlayer
        mod.XimboPlayer = FakePlayer
        try:
            guild.voice_client = player
            player.current = track(1)
            player.queue.clear()
            await player.queue.put_wait([track(2), track(3, stream=True), track(4)])
            i = interaction(guild, owner, channel_id=77)
            await cog.pl_save.callback(cog, i, "Soirée", False)
            assert "3 titres" in said(i)  # le direct est pas gardé
            row = await cog.db.one("SELECT * FROM playlists WHERE owner_id = 5")
            assert row["name"] == "Soirée" and row["count"] == 3 and len(json.loads(row["tracks"])) == 3
            i = interaction(guild, owner, channel_id=77)
            await cog.pl_save.callback(cog, i, "soirée", False)
            assert "remplacer" in said(i)  # même nom (sans tenir compte des majuscules)
            i = interaction(guild, owner, channel_id=77)
            await cog.pl_save.callback(cog, i, "SOIRÉE", True)
            assert "sauvée" in said(i) and (await cog.db.one("SELECT COUNT(*) AS n FROM playlists"))["n"] == 1

            choices = await cog.ac_playlists(SimpleNamespace(user=owner), "soi")
            assert [c.value for c in choices] == ["SOIRÉE"] and "3 titres" in choices[0].name  # renommée en remplaçant

            # Charger : les titres reviennent sans recherche, avec le bon demandeur
            player.queue.clear()
            cog.search = AsyncMock(side_effect=AssertionError("pas de recherche pour une playlist perso"))
            i = interaction(guild, owner, channel_id=77)
            await cog.pl_load.callback(cog, i, "soirée", False, False)
            assert "SOIRÉE" in said(i) and [t.title for t in player.queue] == ["Chanson 1", "Chanson 2", "Chanson 4"]
            assert all(t.extras.requester == 5 for t in player.queue)

            i = interaction(guild, owner, channel_id=77)
            await cog.pl_list.callback(cog, i)
            assert "SOIRÉE" in i.response.send_message.await_args.kwargs["embed"].description
            i = interaction(guild, owner, channel_id=77)
            await cog.pl_delete.callback(cog, i, "SOIRÉE")
            assert "supprimée" in said(i)
            i = interaction(guild, owner, channel_id=77)
            await cog.pl_load.callback(cog, i, "Soirée", False, False)
            assert "Aucune playlist" in said(i)
        finally:
            mod.XimboPlayer = real_player


async def test_autocomplete(tmp_path):
    async with ng_bot(tmp_path, "ximbo") as bot:
        cog = bot.get_cog("Ximbo")
        i = SimpleNamespace(guild_id=1)
        assert await cog.ac_search(i, "") == []
        typed = await cog.ac_search(i, "cowboys")  # Lavalink pas prêt : juste le texte tapé
        assert [c.value for c in typed] == ["cowboys"]
        cog.node_status = "prêt"
        long = track(2)
        long.raw_data["info"]["uri"] = "https://exemple.com/" + "x" * 120
        cog.search = AsyncMock(return_value=[track(1), long])
        choices = await cog.ac_search(i, "cowboys")
        assert choices[0].value == "cowboys" and choices[1].value == "https://youtu.be/id1"
        assert "Chanson 1 — Artiste 1 (3:21)" == choices[1].name
        assert len(choices[2].value) <= 100
        cog.search = AsyncMock(side_effect=AssertionError("doit venir du cache"))
        assert await cog.ac_search(i, "Cowboys") == choices
        assert [c.value for c in await cog.ac_search(i, "https://open.spotify.com/x")] == ["https://open.spotify.com/x"]
