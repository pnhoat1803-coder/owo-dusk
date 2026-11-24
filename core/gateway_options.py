"""What the client asks Discord for when it logs in (phase 4, item K).

The options come from global_settings.json `gateway` (utils/config_models/globalSettings.py, class Gateway). With
gateway.lean false nothing is passed, so discord.py-self keeps its own defaults, which is how the bot ran before.

Only options that the installed library documents are passed. discord.Client takes **options, so the signature does
not list them: the names are read from the parameter list in the Client docstring. An unknown option is skipped with
one debug line.
"""

import logging
import re

import discord

_log = logging.getLogger(__name__)

# A parameter line of the Client docstring: a lower-case name and a colon followed by a space. The indentation is not
# kept in the runtime docstring (discord.Client.__doc__ starts its parameter lines at column 0)
_PARAM = re.compile(r"^\s*([a-z_][a-z0-9_]*):(?=\s)", re.MULTILINE)
_documented = None


def library_options(doc=None):
    """The option names the installed discord.Client documents. doc is only for tests"""
    global _documented
    if doc is not None:
        return frozenset(_PARAM.findall(doc))
    if _documented is None:
        _documented = frozenset(_PARAM.findall(discord.Client.__doc__ or ""))
    return _documented


def gateway_options(gateway, supported=None):
    """Keyword options for discord.Client, built from the gateway settings. An empty dict keeps the library defaults.

    gateway.lean false gives {}: the old behaviour. Otherwise:
    - guild_subscriptions = gateway.guildSubscriptions (default false). Off, discord.py-self does not send its
      per-guild subscription (op 37) at READY and GUILD_CREATE, so no typing, presence or member-list events come
      from servers the bot does not use (state.py:1533 and 2954, subscribe_guild at state.py:1436).
    - chunk_guilds_at_startup = gateway.chunkGuilds (default false). It is forced off without subscriptions, because
      the library refuses that combination (state.py:1056).
    - max_messages = gateway.maxMessages. 0 becomes None (no message cache). The library turns 0 into 1000.
    - request_guilds = gateway.requestGuilds, only for a library that has no guild_subscriptions. In the installed
      library guild_subscriptions wins (state.py:1045) and request_guilds is a deprecated alias, so it is not passed.
    """
    if gateway is None or not getattr(gateway, "lean", False):
        return {}
    names = library_options() if supported is None else frozenset(supported)
    options = {}
    subscriptions = bool(gateway.guildSubscriptions)
    chunk = bool(gateway.chunkGuilds)
    if "guild_subscriptions" in names:
        options["guild_subscriptions"] = subscriptions
        if chunk and not subscriptions:
            _log.warning("gateway.chunkGuilds needs gateway.guildSubscriptions; chunking stays off")
            chunk = False
    else:
        _log.debug("gateway.guildSubscriptions not passed: this discord.py-self has no such option")
        if "request_guilds" in names:
            options["request_guilds"] = bool(gateway.requestGuilds)
        else:
            _log.debug("gateway.requestGuilds not passed: this discord.py-self has no such option")
    if "chunk_guilds_at_startup" in names:
        options["chunk_guilds_at_startup"] = chunk
    else:
        _log.debug("gateway.chunkGuilds not passed: this discord.py-self has no such option")
    if "max_messages" in names:
        options["max_messages"] = None if gateway.maxMessages == 0 else int(gateway.maxMessages)
    else:
        _log.debug("gateway.maxMessages not passed: this discord.py-self has no such option")
    return options
