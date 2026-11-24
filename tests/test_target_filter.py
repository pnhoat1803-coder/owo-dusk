"""Lean gateway (phase 4, item K): which events reach the cogs. core/target_filter.py and the two gates in core/client.py.

No network, no config files: the bot is a stub, and the client is built without MyClient.__init__.
"""

import types
from collections import OrderedDict

import discord.ext.commands as commands

import core.client as client_module
from core import target_filter as tf

SPAM = 111111111111111111
OTHER = 222222222222222222
OWO_DM = 333333333333333333
OWNER = 444444444444444444
ALLOWED = 777777777777777777
STRANGER = 555555555555555555
GUILD = 666666666666666666
OWO = tf.OWO_BOT_ID
REACTION = tf.REACTION_BOT_ID


def ns(**kwargs):
    return types.SimpleNamespace(**kwargs)


class FakeBot:
    def __init__(self, lean=True, boss=False, giveaway=(), custom_channel=None, dm_id=OWO_DM):
        self.gateway_lean = lean
        self.channel_id = SPAM
        self.cm = ns(id=SPAM)
        self.dm = ns(id=dm_id) if dm_id else None
        commands_obj = ns(**{name: ns(custom_channel=None) for name in tf.COMMAND_NAMES})
        if custom_channel is not None:
            commands_obj.hunt = ns(custom_channel=ns(enabled=True, channel=custom_channel))
        self.settings_dict = ns(
            giveaway=ns(enabled=bool(giveaway), channels=list(giveaway)),
            boss=ns(enabled=boss),
            commands=commands_obj,
        )
        self.global_settings_dict = ns(textCommands=ns(allowedUsers=[ALLOWED]))
        self.user = ns(id=OWNER)


def msg(channel, author, guild=True, mid=1):
    """A discord.py-like Message: channel, author, and guild (None in a DM)"""
    return ns(id=mid, channel=ns(id=channel), author=ns(id=author),
              guild=ns(id=GUILD) if guild else None, content="text")


def raw(channel, author, guild=True):
    data = {"id": "1", "channel_id": str(channel), "author": {"id": str(author)}}
    if guild:
        data["guild_id"] = str(GUILD)
    return data


def test_spam_channel_passes_from_anyone():
    bot = FakeBot()
    assert tf.message_wanted(bot, msg(SPAM, STRANGER))
    assert tf.raw_message_wanted(bot, raw(SPAM, STRANGER))


def test_other_server_channel_from_a_stranger_is_dropped():
    bot = FakeBot()
    assert not tf.message_wanted(bot, msg(OTHER, STRANGER))
    assert not tf.raw_message_wanted(bot, raw(OTHER, STRANGER))


def test_owo_dm_passes_and_a_dm_with_another_user_is_dropped():
    bot = FakeBot()
    assert tf.message_wanted(bot, msg(OWO_DM, OWO, guild=False))
    assert tf.raw_message_wanted(bot, raw(OWO_DM, OWO, guild=False))
    assert not tf.message_wanted(bot, msg(9009, STRANGER, guild=False))
    assert not tf.raw_message_wanted(bot, raw(9009, STRANGER, guild=False))


def test_owo_message_in_another_server_channel_is_dropped_unless_boss_is_on():
    assert not tf.message_wanted(FakeBot(boss=False), msg(OTHER, OWO))
    assert tf.message_wanted(FakeBot(boss=True), msg(OTHER, OWO))


def test_owner_and_allowed_users_reach_text_commands_in_any_channel_and_dms():
    bot = FakeBot()
    assert tf.message_wanted(bot, msg(OTHER, OWNER))
    assert tf.message_wanted(bot, msg(OTHER, ALLOWED))
    assert tf.message_wanted(bot, msg(9009, ALLOWED, guild=False))
    assert not tf.message_wanted(bot, msg(OTHER, STRANGER))


def test_reaction_bot_messages_pass_wherever_they_are():
    assert tf.message_wanted(FakeBot(), msg(OTHER, REACTION))


def test_giveaway_channels_pass_only_while_giveaway_is_enabled():
    assert tf.message_wanted(FakeBot(giveaway=[OTHER]), msg(OTHER, STRANGER))
    assert not tf.message_wanted(FakeBot(giveaway=()), msg(OTHER, STRANGER))


def test_custom_channel_of_an_enabled_command_passes():
    assert tf.message_wanted(FakeBot(custom_channel=OTHER), msg(OTHER, STRANGER))
    assert not tf.message_wanted(FakeBot(custom_channel=None), msg(OTHER, STRANGER))


def test_channel_switch_moves_the_spam_channel():
    bot = FakeBot()
    bot.channel_id = OTHER
    bot.cm = ns(id=OTHER)
    assert tf.message_wanted(bot, msg(OTHER, STRANGER))
    assert not tf.message_wanted(bot, msg(SPAM, STRANGER))


def test_lean_off_passes_everything():
    bot = FakeBot(lean=False)
    assert tf.message_wanted(bot, msg(OTHER, STRANGER))
    assert tf.raw_message_wanted(bot, raw(OTHER, STRANGER))
    assert tf.event_wanted(bot, "message_edit", (msg(OTHER, STRANGER), msg(OTHER, STRANGER)))


def test_partial_edit_without_author_only_passes_in_dms():
    bot = FakeBot()
    assert not tf.raw_message_wanted(bot, {"channel_id": str(OTHER), "guild_id": str(GUILD)})
    assert tf.raw_message_wanted(bot, {"channel_id": str(9009)})


def test_captcha_warning_reaches_the_listeners_in_the_dm_and_in_the_spam_channel(monkeypatch):
    """Both captcha paths of OwO keep working: the DM and the spam channel reach the listeners, another channel does not"""

    class Probe(client_module.MyClient):
        # discord.Client owns `user`; the probe reads it from an attribute instead
        user = property(lambda self: self._owner)

    reached = []

    def listeners_chain(self, event, *args, **kwargs):
        for message in args:
            if "captcha" in message.content.lower():
                reached.append((message.channel.id, message.guild is None))

    monkeypatch.setattr(commands.Bot, "dispatch", listeners_chain, raising=False)
    bot = FakeBot()
    client = object.__new__(Probe)
    client.__dict__.update(bot.__dict__)
    client._owner = ns(id=OWNER)
    client.reply_fallback = ns(note_dm_message=lambda message: None)
    client._recent_message_ids = OrderedDict()

    def captcha(channel, guild, author=OWO, mid=1):
        m = msg(channel, author, guild=guild, mid=mid)
        m.content = "Captcha! Please solve it"
        return m

    client.dispatch("message", captcha(OWO_DM, guild=False, mid=1))
    client.dispatch("message", captcha(SPAM, guild=True, mid=2))
    client.dispatch("message", captcha(OTHER, guild=True, mid=3))
    assert reached == [(OWO_DM, True), (SPAM, False)]


def test_message_edit_gate_uses_the_edited_message(monkeypatch):
    class Probe(client_module.MyClient):
        user = property(lambda self: self._owner)

    calls = []
    monkeypatch.setattr(commands.Bot, "dispatch", lambda self, event, *a, **k: calls.append(event), raising=False)
    client = object.__new__(Probe)
    client.__dict__.update(FakeBot().__dict__)
    client._owner = ns(id=OWNER)
    client.reply_fallback = ns(note_dm_message=lambda message: None)
    client._recent_message_ids = OrderedDict()
    client.dispatch("message_edit", msg(OTHER, STRANGER, mid=5), msg(OTHER, STRANGER, mid=5))
    client.dispatch("message_edit", msg(SPAM, STRANGER, mid=6), msg(SPAM, STRANGER, mid=6))
    assert calls == ["message_edit"]
