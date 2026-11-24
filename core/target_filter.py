"""Which gateway events an account reads (phase 4, item K).

With gateway.lean on, an account reads only what it farms with:
- its spam channel (bot.channel_id and bot.cm, both follow a channel switch);
- the DM with the OwO bot (captcha warnings and replies come there too);
- channels that an enabled feature names in its own config: giveawayJoiner.channelsToJoin, and a command's
  customChannel.channelId while that customChannel is enabled;
- OwO bot messages in any server channel, only while bossBattle is enabled (boss battles can be in any channel);
- messages of the reaction bot, wherever they are (its cog has no channel check of its own);
- messages of the owner and of textCommands.allowedUsers, in any channel and in DMs (text commands from them).

The gate runs twice, and both runs happen before the cogs see anything:
- core/client.py on_socket_raw_receive: on the raw MESSAGE_CREATE / MESSAGE_UPDATE payload, before a Message object
  is built (the frame is already parsed there);
- core/client.py dispatch(): for 'message', 'message_edit' and 'raw_message_edit', before any cog listener runs.
The library still parses every frame and keeps its own message cache (max_messages), so this saves listener work,
not inbound bytes. Inbound bytes come from what Discord sends, see gateway_options.py.

With gateway.lean false every function here returns True, so nothing changes.
"""

OWO_BOT_ID = 408785106942164992
REACTION_BOT_ID = 519287796549156864
COMMAND_NAMES = ("hunt", "battle", "pray", "curse", "lvlGrind", "cookie", "owo", "army", "pup", "piku", "run")


def lean_on(bot):
    return bool(getattr(bot, "gateway_lean", False))


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _spam_ids(bot):
    ids = set()
    channel_id = _as_int(getattr(bot, "channel_id", None))
    if channel_id is not None:
        ids.add(channel_id)
    cm_id = _as_int(getattr(getattr(bot, "cm", None), "id", None))
    if cm_id is not None:
        ids.add(cm_id)
    return ids


def _feature_ids(bot):
    """Channel ids that enabled features name in their config. Reads settings only, never the network"""
    ids = set()
    settings = getattr(bot, "settings_dict", None)
    if settings is None:
        return ids
    giveaway = getattr(settings, "giveaway", None)
    if giveaway is not None and getattr(giveaway, "enabled", False):
        for channel in getattr(giveaway, "channels", None) or []:
            channel_id = _as_int(channel)
            if channel_id is not None:
                ids.add(channel_id)
    commands = getattr(settings, "commands", None)
    for name in COMMAND_NAMES:
        custom = getattr(getattr(commands, name, None), "custom_channel", None)
        if custom is not None and getattr(custom, "enabled", False):
            channel_id = _as_int(getattr(custom, "channel", None))
            if channel_id is not None:
                ids.add(channel_id)
    return ids


def _boss_on(bot):
    boss = getattr(getattr(bot, "settings_dict", None), "boss", None)
    return bool(getattr(boss, "enabled", False))


def _owner_ids(bot):
    """The account's own id, and the allowed users of the text commands"""
    ids = set()
    user_id = _as_int(getattr(getattr(bot, "user", None), "id", None))
    if user_id is not None:
        ids.add(user_id)
    text = getattr(getattr(bot, "global_settings_dict", None), "textCommands", None)
    for user in getattr(text, "allowedUsers", None) or []:
        user_id = _as_int(user)
        if user_id is not None:
            ids.add(user_id)
    return ids


def _owo_dm_id(bot):
    return _as_int(getattr(getattr(bot, "dm", None), "id", None))


def wanted(bot, channel_id, in_guild, author_id):
    """True when an event from this channel and author should reach the cogs.

    in_guild says whether the channel belongs to a server (False for a DM). author_id None means the payload has no
    author (a partial edit): then only DMs pass, because a server message can only be wanted in a channel named above.
    """
    if not lean_on(bot):
        return True
    channel_id = _as_int(channel_id)
    author_id = _as_int(author_id)
    if channel_id is None:
        return True  # cannot tell where it is from: keep it, the cogs check their own channel
    if channel_id in _spam_ids(bot) or channel_id in _feature_ids(bot):
        return True
    if author_id is None:
        return not in_guild
    if not in_guild:
        return author_id == OWO_BOT_ID or channel_id == _owo_dm_id(bot) or author_id in _owner_ids(bot)
    if author_id == OWO_BOT_ID:
        return _boss_on(bot)
    if author_id == REACTION_BOT_ID:
        return True
    return author_id in _owner_ids(bot)


def raw_message_wanted(bot, data):
    """The same rule for the raw payload of MESSAGE_CREATE / MESSAGE_UPDATE (the 'd' object)"""
    if not lean_on(bot) or not isinstance(data, dict):
        return True
    author = data.get("author")
    author_id = author.get("id") if isinstance(author, dict) else None
    return wanted(bot, data.get("channel_id"), data.get("guild_id") is not None, author_id)


def message_wanted(bot, message):
    """The same rule for a discord.py Message object"""
    if not lean_on(bot):
        return True
    channel_id = getattr(getattr(message, "channel", None), "id", None)
    if channel_id is None:
        channel_id = getattr(message, "channel_id", None)
    in_guild = getattr(message, "guild", None) is not None or getattr(message, "guild_id", None) is not None
    return wanted(bot, channel_id, in_guild, getattr(getattr(message, "author", None), "id", None))


def event_wanted(bot, event, args):
    """The gate used by Client.dispatch. Events the cogs do not listen to to pass unchanged"""
    if not lean_on(bot) or not args:
        return True
    if event == "message":
        return message_wanted(bot, args[0])
    if event == "message_edit":
        return message_wanted(bot, args[-1])  # (before, after): the edited message is the last one
    if event == "raw_message_edit":
        payload = args[0]
        guild_id = getattr(payload, "guild_id", None)
        return wanted(bot, getattr(payload, "channel_id", None), guild_id is not None, None)
    return True
