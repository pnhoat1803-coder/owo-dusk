"""Lean gateway (phase 4, item K): the options the client is built with, and the settings they come from."""

import logging

import discord

from core.gateway_options import gateway_options, library_options
from utils.config_models.globalSettings import Gateway
from utils.config_models.settings import Command

ALL_NEW = {"guild_subscriptions", "request_guilds", "chunk_guilds_at_startup", "max_messages"}


def test_installed_library_documents_the_gateway_options():
    names = library_options()
    for name in ("guild_subscriptions", "request_guilds", "chunk_guilds_at_startup", "max_messages"):
        assert name in names, name


def test_defaults_of_the_gateway_block():
    gw = Gateway({})
    assert gw.lean is True
    assert gw.chunkGuilds is False
    assert gw.guildSubscriptions is False
    assert gw.maxMessages == 200
    assert gw.requestGuilds is True


def test_gateway_block_is_optional_in_the_global_settings():
    # An old global_settings.json has no gateway key: GlobalSettings still reads the defaults
    assert Gateway(None).lean is True
    assert Gateway("not a dict").maxMessages == 200


def test_bad_values_keep_their_defaults():
    gw = Gateway({"lean": "yes", "maxMessages": 5000, "chunkGuilds": 1})
    assert gw.lean is True
    assert gw.maxMessages == 200
    assert gw.chunkGuilds is False


def test_lean_false_passes_nothing_so_the_library_defaults_apply():
    assert gateway_options(Gateway({"lean": False, "guildSubscriptions": True})) == {}
    assert gateway_options(None) == {}


def test_lean_on_passes_the_documented_options_with_the_defaults():
    options = gateway_options(Gateway({}), supported=ALL_NEW)
    assert options == {"guild_subscriptions": False, "chunk_guilds_at_startup": False, "max_messages": 200}
    assert "request_guilds" not in options  # guild_subscriptions wins in the library (state.py:1045)


def test_zero_messages_becomes_none_because_the_library_turns_zero_into_1000():
    assert gateway_options(Gateway({"maxMessages": 0}), supported=ALL_NEW)["max_messages"] is None


def test_chunking_is_forced_off_without_subscriptions(caplog):
    with caplog.at_level(logging.WARNING):
        options = gateway_options(Gateway({"chunkGuilds": True, "guildSubscriptions": False}), supported=ALL_NEW)
    assert options["chunk_guilds_at_startup"] is False
    assert any("chunkGuilds" in record.getMessage() for record in caplog.records)


def test_chunking_with_subscriptions_is_passed_through():
    options = gateway_options(Gateway({"chunkGuilds": True, "guildSubscriptions": True}), supported=ALL_NEW)
    assert options["chunk_guilds_at_startup"] is True
    assert options["guild_subscriptions"] is True


def test_an_older_library_gets_request_guilds_instead():
    old = {"request_guilds", "chunk_guilds_at_startup", "max_messages"}
    options = gateway_options(Gateway({"requestGuilds": False}), supported=old)
    assert options["request_guilds"] is False
    assert "guild_subscriptions" not in options


def test_unknown_options_are_skipped_without_error(caplog):
    with caplog.at_level(logging.DEBUG):
        assert gateway_options(Gateway({}), supported=set()) == {}
    assert any("not passed" in record.getMessage() for record in caplog.records)


def test_installed_library_accepts_every_option_it_is_given():
    """The kwargs really are accepted: discord.Client takes them and the state reads them"""
    options = gateway_options(Gateway({"maxMessages": 50}))
    client = discord.Client(**options)
    assert client._connection.max_messages == 50
    assert client._connection._chunk_guilds is False
    assert client._connection._subscribe_guilds is False


def test_quote_source_defaults_to_api_and_rejects_other_values(caplog):
    assert Command({"enabled": True}).quoteSource == "api"
    assert Command({"enabled": True, "quoteSource": "file"}).quoteSource == "file"
    with caplog.at_level(logging.WARNING):
        assert Command({"enabled": True, "quoteSource": "favqs"}).quoteSource == "api"
    assert any("quoteSource" in record.getMessage() for record in caplog.records)

