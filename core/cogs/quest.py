# This file is part of owo-dusk.
#
# Copyright (c) 2024-present EchoQuill
#
# Portions of this file are based on code by EchoQuill, licensed under the
# GNU General Public License v3.0 (GPL-3.0).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path

from discord.ext import commands, tasks
from discord.ext.commands import ExtensionNotLoaded

from core.auto_stop import clients_snapshot, stop_requested
from core.cogs._BASE import BaseCog
from utils.quest_helper.quest_store import QuestStore

logger = logging.getLogger(__name__)

# Quest safety limits (phase A). They stop command spam: change them only with a reason
QUEST_MIN_CHECK_SECONDS = 600          # never send "owo quest" more often than this
QUEST_DONE_FALLBACK_SECONDS = 4 * 3600 # wait this long after "all quests done" if OwO gives no time
QUEST_MAX_COMMANDS_PER_HOUR = 60       # hard cap for ALL commands sent by quest code, per account
QUEST_REPEAT_MAX_COMMANDS = 30         # max commands in one repeat run without a known count
QUEST_REPEAT_MAX_SECONDS = 30 * 60     # max time of one repeat run
QUEST_ERROR_BACKOFF_SECONDS = 120      # sleep after an unexpected error in the loop
QUEST_SLEEP_CHUNK_SECONDS = 60         # long waits are cut into chunks this long, so a stop is seen quickly
QUEST_LOG_REPLY_SECONDS = 90           # after "owo quest": wait this long for this account's Quest Log (B7; D4: was 30)
QUEST_ECHO_MISSES_STOP = 3             # this many commands in a row without their echo end a run (B3)
QUEST_DEBUG_MAX_LINES = 50             # utils/data/quest_debug.jsonl keeps only its newest lines (B10)
QUEST_ALL_DONE_TEXT = "UwU You finished all of your quests!"  # OwO's text when every quest of the day is done
QUEST_HELP_WAIT_MAX_SECONDS = 15 * 60   # max wait for a helped quest (C1)
QUEST_CLAIM_TTL_SECONDS = 30 * 60       # a claim expires after this (C6)
QUEST_FIRST_CHECK_SPREAD_SECONDS = 900  # D4: the first check after start (and after an all-done wait) waits U(0, this) more
QUEST_OCR_REUSE_SECONDS = 30 * 60       # D3: no OCR again for the same next-quest time within this, when nothing can be claimed

# Clocks, looked up at call time so tests can replace them
_monotonic = time.monotonic  # intervals and limits
_wall_now = time.time  # Discord timestamps are wall-clock Unix time

_DISCORD_TIMESTAMP = re.compile(r"<t:(\d+)(?::[a-zA-Z])?>")


def _text_contents(components):
    """Text of every text display of a component list, sections and containers included (recursive)"""
    texts = []
    for comp in components or []:
        content = getattr(comp, "content", None)
        if isinstance(content, str) and content:
            texts.append(content)
        children = getattr(comp, "components", None)
        if isinstance(children, (list, tuple)):
            texts.extend(_text_contents(children))
    return texts


def _all_texts(message):
    """B9: every text of a message: its content, every component text, every embed title, description and field"""
    texts = []
    content = getattr(message, "content", None)
    if isinstance(content, str) and content:
        texts.append(content)
    texts.extend(_text_contents(getattr(message, "components", None)))
    for embed in getattr(message, "embeds", None) or []:
        values = [getattr(embed, "title", None), getattr(embed, "description", None)]
        for field in getattr(embed, "fields", None) or []:
            values.extend([getattr(field, "name", None), getattr(field, "value", None)])
        texts.extend(value for value in values if isinstance(value, str) and value)
    return texts


def _next_quest_timestamp_from(texts, any_text=False):
    """Unix time of the next quest, from the texts of a quest message, or None.
    Accepts <t:N:f>, <t:N:R> and every other style. any_text=True (all quests done) also looks at the rest of the text"""
    preferred = [text for text in texts if "next quest" in text.lower()]
    candidates = preferred
    if any_text:
        candidates = preferred + [text for text in texts if text not in preferred]
    for text in candidates:
        found = _DISCORD_TIMESTAMP.search(text)
        if found:
            return int(found.group(1))
    return None


_NEXT_QUEST = re.compile(r"Next quest:\s*\*{0,2}<t:(\d+)(?::[a-zA-Z])?>\*{0,2}", re.IGNORECASE)
_SEALS = re.compile(r"Balance:\s*\*{0,2}([\d,]+)\*{0,2}[^\n]*?Quest Seals", re.IGNORECASE)


def _seals_from(texts):
    """D6: the Quest Seals balance of a Quest Log ("Balance: **10** <:seal:…> Quest Seals"), bold optional, or None"""
    for text in texts:
        found = _SEALS.search(text)
        if found:
            return int(found.group(1).replace(",", ""))
    return None


def _next_quest_from_log(texts):
    """D6: the next-quest time of a Quest Log: "Next quest: **<t:N:R>**" (bold optional) first, then the B9 fallback"""
    for text in texts:
        found = _NEXT_QUEST.search(text)
        if found:
            return int(found.group(1))
    return _next_quest_timestamp_from(texts)


_MENTION_OR_ID = re.compile(r"<@!?(\d+)>|(?<!\d)(\d{17,20})(?!\d)")


def _mask_ids(text):
    """B10: every Discord user id or mention is written as <@…last4>"""
    return _MENTION_OR_ID.sub(lambda m: f"<@…{(m.group(1) or m.group(2))[-4:]}>", text)


def _component_tree(components):
    """B10: the component types only, as a tree"""
    tree = []
    for comp in components or []:
        node = {"type": getattr(comp, "component_name", "?")}
        children = getattr(comp, "components", None)
        if isinstance(children, (list, tuple)) and children:
            node["children"] = _component_tree(children)
        tree.append(node)
    return tree


def _image_urls(components):
    """B10: the image urls of a message (the query string is removed by the caller)"""
    urls = []
    for comp in components or []:
        for item in getattr(comp, "items", None) or []:
            url = getattr(getattr(item, "media", None), "url", None)
            if isinstance(url, str):
                urls.append(url)
        children = getattr(comp, "components", None)
        if isinstance(children, (list, tuple)):
            urls.extend(_image_urls(children))
    return urls


def _in_guild(client, guild_id) -> bool:
    """C8: whether this client is in the server. A client that cannot tell (a test double without get_guild) counts as in"""
    if guild_id is None:
        return True
    getter = getattr(client, "get_guild", None)
    if not callable(getter):
        return True
    return getter(guild_id) is not None


def _quest_debug_path():
    """B10: utils/data/quest_debug.jsonl. With DUSK_LOG_DIR set (the tests set it) it goes into that folder instead"""
    override = os.environ.get("DUSK_LOG_DIR", "").strip()
    if override:
        return Path(override).resolve() / "quest_debug.jsonl"
    return Path(__file__).resolve().parents[2] / "utils" / "data" / "quest_debug.jsonl"


def _append_quest_debug(path, record, max_lines):
    """Appends one JSON line and keeps only the newest max_lines lines"""
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    lines.append(json.dumps(record, ensure_ascii=False))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")

try:
    with open("utils/data/emojis.json", "r", encoding="utf-8") as file:
        emoji_dict = json.load(file)
except FileNotFoundError:
    print("The file emojis.json was not found.")
except json.JSONDecodeError:
    print("Failed to decode JSON from the file.")


def has_ranked_emoji(self, text, rank, emoji_dict=emoji_dict):
    """
    Takes message and rank to figure out if rank exists in the hunt message
    """
    pattern = re.compile(
        r"<a:[a-zA-Z0-9_]+:[0-9]+>|:[a-zA-Z0-9_]+:|[\U0001F300-\U0001F6FF\U0001F700-\U0001F77F]"
    )
    emojis = pattern.findall(text)

    for emoji in emojis:
        emoji_data = emoji_dict.get(emoji)
        # Check if rank matches that of emoji_dict
        if emoji_data and emoji_data["rank"] == rank:
            return True

    return False


animal_ranks = [
    "common",
    "uncommon",
    "rare",
    "epic",
    "special",
    "mythical",
    "gem",
    "legendary",
    "fabled",
    "distorted",
    "hidden",
]

# NOTE: Never use quest details fetched as a means to validate
# they may be edited in certain cases, for eg. when multiple of same quest
# inorder to work properly

"""

UPDATE ongoing_battle_external_quest FLAG B4 RELEASE!
"""


class Quest(BaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.timed_task = None  # track active timed task so we can cancel it
        self.quest_cmd = {
            "cmd_name": self.bot.alias["quest"]["normal"],
            "prefix": True,
            "checks": True,
            "id": "quest",
        }
        self.all_quests_done = False
        self.alr_posted = False
        self.bot.message_dispatcher.register(self.on_component_message)
        self.catcha_a_rank = ""
        self.quest_self_doable = {
            "battle_xp": {"quest": False, "till": 0},
            "gamble": {"quest": False, "till": 0},
            "boss": {"quest": False, "till": 0},
            "action_send": {"quest": False, "till": 0},
            "hunt": {"quest": False, "till": 0},
            "battle": {"quest": False, "till": 0},
            "owo": {"quest": False, "till": 0},
        }
        for rank in animal_ranks:
            self.quest_self_doable[f"find_animal_{rank}"] = {"quest": False, "till": 0}

        self.repeat_quest_flag = False
        # Safety state (phase A)
        self._last_quest_check = None  # monotonic time of the last "owo quest" send
        self._quest_sent = deque()  # monotonic times of the commands quest code sent, last hour
        self._hourly_pause_logged = False
        self._active_run = None  # key of the own quest whose run is going on (B6): only that run stops on its counter
        self._quest_log_event = asyncio.Event()  # set when this account's Quest Log was processed (B7)
        # D5: the quest state is kept only when the cog starts for real (cog_load). Tests call enable_state_store
        self.store = None
        self._store_failed = False  # the first store failure is logged, later ones are not
        self._spread_pending = True  # D4: the next check is spread (cleared when that check runs)
        self._last_log_at = None  # wall time of the last Quest Log that was processed
        self._last_ocr_ok_at = None  # wall time of the last good OCR, and the next-quest time that read belonged to
        self._last_ocr_next_at = None
        self._last_claim_at = None  # wall time of the last claim of a finished quest (D5, D7)
        self._claims_total = 0

    async def cog_unload(self):
        # Stop listening and stop the loop, so nothing keeps running after the cog is unloaded
        self.bot.message_dispatcher.unregister(self.on_component_message)
        self.main_quest_loop.cancel()

    def _should_stop(self) -> bool:
        """The same check CommandHandler.put_queue makes: this account is stopped, or autoStop has started"""
        return bool(getattr(self.bot, "stopped", False)) or stop_requested()

    async def _sleep_checked(self, seconds) -> bool:
        """Sleeps in chunks of QUEST_SLEEP_CHUNK_SECONDS. False when the account stopped meanwhile"""
        end = _monotonic() + max(0.0, float(seconds))
        while True:
            if self._should_stop():
                return False
            left = end - _monotonic()
            if left <= 0:
                return True
            await asyncio.sleep(min(QUEST_SLEEP_CHUNK_SECONDS, left))

    async def _cap_wait(self) -> bool:
        """The hourly cap: waits until one more command may go out. False when the account stopped meanwhile"""
        while True:
            now = _monotonic()
            while self._quest_sent and now - self._quest_sent[0] >= 3600:
                self._quest_sent.popleft()
            if len(self._quest_sent) < QUEST_MAX_COMMANDS_PER_HOUR:
                self._hourly_pause_logged = False
                return True
            if not self._hourly_pause_logged:
                self._hourly_pause_logged = True
                await self.bot.log(
                    "[quest] hourly cap reached, pausing quest commands",
                    color="#e6e65a",
                )
            # wait until the oldest send is more than an hour old
            if not await self._sleep_checked(3600 - (now - self._quest_sent[0]) + 1):
                return False

    async def _quest_put(self, cmd_dict):
        """Queues one command and records its send time for the hourly cap"""
        await self.bot.ch.put_queue(cmd_dict)
        self._quest_sent.append(_monotonic())

    async def _quest_send(self, cmd_dict) -> bool:
        """The way quest code queues a command: the hourly cap comes first (QUEST_MAX_COMMANDS_PER_HOUR)"""
        if not cmd_dict:
            return False
        if not await self._cap_wait():
            return False
        await self._quest_put(cmd_dict)
        return True

    async def send_capped(self, cmd_dict) -> bool:
        """For the other cogs' quest help sends (battle, pray, curse): the same hourly cap as the quest commands (C2, C3)"""
        return await self._quest_send(cmd_dict)

    async def _send_and_confirm(
        self, cmd_name, arg=None, prefix=True, channel=None, queue_id=None
    ) -> str:
        """
        Queues one command and waits for its echo (our own message). Returns "echo", "miss" (no echo in time: the
        queue dropped it as a duplicate, or OwO did not show it) or "stopped". Only "echo" is progress (B3).
        """
        cmd = {
            "cmd_name": cmd_name,
            "cmd_arguments": arg,
            "prefix": prefix,
            "checks": True,
            "id": queue_id or cmd_name,
            "channel": channel,
        }
        if not await self._cap_wait():
            return "stopped"
        echo = asyncio.ensure_future(
            self.block_till_send(cmd_name, channel, prefix, arg)
        )
        try:
            # the echo listener is in place before the command can go out
            await asyncio.sleep(0)
            await self._quest_put(cmd)
            seen = await echo
        except BaseException:
            echo.cancel()
            raise
        # the check entry is dropped once our own message showed up, so monitor_checks cannot queue it again
        await self.bot.ch.remove_queue(id=cmd["id"])
        return "echo" if seen else "miss"

    async def _wait_for_quest_log(self) -> bool:
        """B7: True once this account's Quest Log was processed, False after QUEST_LOG_REPLY_SECONDS (stops are seen)"""
        started = _monotonic()
        while not self._quest_log_event.is_set():
            if self._should_stop():
                return False
            waited = _monotonic() - started
            if waited >= QUEST_LOG_REPLY_SECONDS:
                return False
            await asyncio.sleep(min(1.0, QUEST_LOG_REPLY_SECONDS - waited))
        return True

    async def _run_self_quest(self, key, cmd_name, arg=None, till=None) -> bool:
        """
        One run of one of this account's own quests (phase B).
        - till given (hunt, owo, gamble): every command whose echo is seen counts 1 (B3).
        - till None (battle, battle_xp, find_animal_<rank>): OwO's own results count, in _handle_message, and the run
          stops when that counter reaches 0 (repeat_quest_flag). Such a run also stops after
          QUEST_REPEAT_MAX_COMMANDS commands.
        Every run also stops after QUEST_REPEAT_MAX_SECONDS, on a stop of the account, and after QUEST_ECHO_MISSES_STOP
        commands in a row without echo. Its flags are reset when it ends (B6).
        """
        counted_by_results = till is None
        remaining = 0 if counted_by_results else int(till or 0)
        started = _monotonic()
        sent = 0
        misses = 0
        self._active_run = key
        self.repeat_quest_flag = False
        try:
            while True:
                if self._should_stop():
                    break
                if counted_by_results:
                    if self.repeat_quest_flag or self.quest_self_doable[key]["till"] <= 0:
                        break
                elif remaining <= 0:
                    break
                cnf = self.get_cmd_cnf(cmd_name)
                if cnf.enabled:
                    # the player started this command himself: this run ends
                    break
                if _monotonic() - started >= QUEST_REPEAT_MAX_SECONDS or (
                    counted_by_results and sent >= QUEST_REPEAT_MAX_COMMANDS
                ):
                    await self.bot.log(
                        f"[quest] {key} repeat limit reached, will re-check quest log",
                        color="#e6e65a",
                    )
                    break
                arg_now = "1" if cmd_name in {"slots", "coinflip"} else arg
                outcome = await self._send_and_confirm(
                    cmd_name, arg=arg_now, prefix=cmd_name != "owo"
                )
                if outcome == "stopped":
                    break
                sent += 1
                if outcome == "echo":
                    misses = 0
                    if not counted_by_results:
                        remaining -= 1
                else:
                    misses += 1
                    if misses >= QUEST_ECHO_MISSES_STOP:
                        await self.bot.log(
                            f"[quest] {key} {QUEST_ECHO_MISSES_STOP} commands without echo, stopping run",
                            color="#e6e65a",
                        )
                        break
                if not await self._sleep_checked(cnf.get_cd()):
                    break
        finally:
            self._active_run = None
            self.repeat_quest_flag = False
            self.catcha_a_rank = ""
            if key in self.quest_self_doable:
                self.quest_self_doable[key]["quest"] = False
        return True

    async def _handle_own_action_quest(self, till, channelid=None):
        """The own action_send quest (B2, B3): only actions whose echo was seen count. The target is another account"""
        target = self._pick_partner_id(self.bot.cm.guild.id)  # C8: a partner that can see this server
        if not target:
            if self._partner_ids():
                await self.bot.log("[quest] no partner in this server", color="#e6e65a")
            else:
                await self.bot.log(
                    "[quest] action quest skipped: no partner account", color="#e6e65a"
                )
            return
        if not self.settings.canEnable:
            return
        await self.bot.log(
            f"Attempting to do quest - sending actions x{till}", color="#a5c7e3"
        )
        actions = ["stare", "kill", "greet", "punch", "wave", "slap"]
        arg = f"<@{target}>"
        remaining = int(till or 0)
        misses = 0
        started = _monotonic()
        try:
            while remaining > 0:
                if self._should_stop():
                    break
                if _monotonic() - started >= QUEST_REPEAT_MAX_SECONDS:
                    await self.bot.log(
                        "[quest] action_send repeat limit reached, will re-check quest log",
                        color="#e6e65a",
                    )
                    break
                outcome = await self._send_and_confirm(
                    self.bot.random.choice(actions),
                    arg=arg,
                    prefix=True,
                    channel=channelid,
                    queue_id="action",
                )
                if outcome == "stopped":
                    break
                if outcome == "echo":
                    misses = 0
                    remaining -= 1
                else:
                    misses += 1
                    if misses >= QUEST_ECHO_MISSES_STOP:
                        await self.bot.log(
                            f"[quest] action_send {QUEST_ECHO_MISSES_STOP} commands without echo, stopping run",
                            color="#e6e65a",
                        )
                        break
                await self.bot.sleep_till([4, 6])
        finally:
            # no cog reads action replies: an action check entry must never stay behind for monitor_checks
            await self.bot.ch.remove_queue(id="action")

    def _dump_quest_debug(self, texts, components, quest_details):
        """B10: one raw sample per quest message, ids masked, the newest QUEST_DEBUG_MAX_LINES kept. Never raises"""
        try:
            record = {
                "time": round(_wall_now(), 3),
                "account": str(self.bot.user.id)[-4:],
                "texts": [_mask_ids(text) for text in texts],
                "tree": _component_tree(components),
                "images": [url.split("?")[0] for url in _image_urls(components)],
                "all_done": bool(quest_details and quest_details["questDone"]),
                "quests": [
                    {
                        "id": quest["quest_id"],
                        "current": quest["current"],
                        "total": quest["total"],
                    }
                    for quest in self.bot.quest_handler.quests
                ],
            }
            _append_quest_debug(_quest_debug_path(), record, QUEST_DEBUG_MAX_LINES)
        except Exception as error:
            logger.warning("quest_debug.jsonl was not written: %s", error)

    def _partner_ids(self, guild_id=None) -> list:
        """Discord ids of the other accounts running in this process. Never this account, never a stopped one.
        With guild_id (C8): only the accounts in that server, so they can see its channels"""
        own_id = getattr(self.bot.user, "id", None)
        ids = set()
        for other in clients_snapshot().values():
            if other is self.bot or getattr(other, "stopped", False):
                continue
            other_id = getattr(getattr(other, "user", None), "id", None)
            if not other_id or other_id == own_id:
                continue
            if guild_id is not None and not _in_guild(other, guild_id):
                continue
            ids.add(other_id)
        return sorted(ids)

    def _pick_partner_id(self, guild_id=None):
        ids = self._partner_ids(guild_id)
        return self.bot.random.choice(ids) if ids else None

    async def _log_unexpected(self, where: str):
        """An unexpected error must not stop the quest loop or other handlers: log it with its traceback"""
        logger.exception("quest: unexpected error in %s", where)
        try:
            await self.bot.log(
                f"[quest] unexpected error in {where}, see the log", color="#c25560"
            )
        except Exception:
            pass

    def get_quest_cmd(self, channel=None):
        cmd = self.quest_cmd.copy()
        if not channel:
            return cmd
        else:
            cmd["channel"] = channel
            return cmd  # B8: a help channel used to get None here

    @property
    def settings(self):
        return self.bot.settings_dict.autoQuest

    @property
    def is_gamble_enabled(self):
        sett = self.bot.settings_dict.gamble
        cf = sett.coinflip.enabled
        slots = sett.slots.enabled
        bj = sett.blackjack.enabled
        return cf or slots or bj

    def _account_allowed(self) -> bool:
        """C0: auto_quest_accounts in config/danger.toml: "*" for all, this account's full id, or its last 4 digits"""
        entries = self.bot.danger_settings_dict.get("auto_quest_accounts", [])
        if not isinstance(entries, list):
            return False
        if "*" in entries:
            return True
        user_id = str(self.bot.user.id)
        return any(str(entry).strip() in {user_id, user_id[-4:]} for entry in entries)

    async def cog_load(self):
        # its not smart to use `await`able functions here, double check!
        if (
            not self.settings.enabled
            or not self.bot.danger_settings_dict["allow_auto_quest"]
        ):
            if not self.bot.danger_settings_dict["allow_auto_quest"]:
                await self.bot.log(
                    "Current Auto quests is in a experimental stage. Inorder to avoid issues, we have locked auto quest until user manually alows it from `owo-dusk/config/danger.toml` file. Check the file for more details!",
                    "#e6e65a",
                )
            try:
                asyncio.create_task(self.bot.unload_cog("core.cogs.quest"))
            except ExtensionNotLoaded:
                pass
        elif not self._account_allowed():
            # C0: only the accounts in auto_quest_accounts run autoquest (and so only they help others)
            await self.bot.log(
                "[quest] autoquest not enabled for this account (allowlist)",
                "#e6e65a",
            )
            try:
                asyncio.create_task(self.bot.unload_cog("core.cogs.quest"))
            except ExtensionNotLoaded:
                pass
        else:
            reaction_bot_settings = self.bot.settings_dict.cooldowns.reactionBot
            if (
                reaction_bot_settings.huntAndBattle
                or reaction_bot_settings.owo
                or reaction_bot_settings.prayAndCurse
            ):
                await self.bot.log(
                    "Currently `helpOthers` doesn't work with reactionbot. There were some issues with reaction bot and quest. This will be doable once `autoQuest` comes out of experimental mode in future updates. You may continue using as is, but it would be better to disable reactionbot while using autoQuest for the best experience!",
                    "#e6e65a",
                )
            self.enable_state_store()  # D5: the real start keeps the quest state
            await self.restore_quest_state()
            self.main_quest_loop.start()

    def is_valid_quest(self, components):
        section_components = None
        for comp in components:
            if comp.component_name == "section":
                section_components = comp.components
                break

        if not section_components:
            return False

        for comp in section_components:
            if comp.content and f"<@{self.bot.user.id}>'s Quest Log" in comp.content:
                return True

        return False

    def get_quest_url(self, components):
        for comp in components:
            if comp.component_name == "media_gallery":
                url = comp.items[0].media.url
                if "quest-rows" in url:
                    return url
        return None

    def get_available_quest_details(self, components, buttons, texts=None):
        """
        Checks if quest is valid first, If valid then it
        checks if there is any claimable or doable quests available.
        texts: every text of the message (see _all_texts); the texts of the components when not given
        """
        if texts is None:
            texts = _text_contents(components)
        res = {
            "claimBtn": None,
            "url": "",
            "questDone": False,
            "next_quest_timestamp": 0,
        }

        # Check for available quests
        for btn in buttons:
            if btn.custom_id == "quests:claim":
                if not btn.disabled:
                    # Quests claimable
                    res["claimBtn"] = btn

        # Check if quest quest is completed for the day: any text of the message (B9)
        res["questDone"] = any(QUEST_ALL_DONE_TEXT in text for text in texts)

        # Next-quest time, read from the same message: the "Next quest" line first (D6), any Discord timestamp (B9)
        res["next_quest_timestamp"] = _next_quest_from_log(texts) or 0
        if res["questDone"] and not res["next_quest_timestamp"]:
            res["next_quest_timestamp"] = _next_quest_timestamp_from(texts, any_text=True) or 0
        # D6: the Quest Seals balance of this message, None when it does not show one
        res["seals"] = _seals_from(texts)

        if not res["questDone"]:
            # Fetch quest Image url
            url = self.get_quest_url(components)
            res["url"] = url

        return res

    """def is_quests_updated(self):
        pass"""

    async def handle_self_quests(self, quests):
        """Do quests we can do ourselfs, Blocking.."""
        # B6: every round starts from a clean state
        for key in self.quest_self_doable:
            self.quest_self_doable[key] = {"quest": False, "till": 0}
        self.catcha_a_rank, self.repeat_quest_flag, self._active_run = "", False, None

        # Update quest stats
        for quest in quests:
            quest_id = quest["quest_id"]

            self.quest_self_doable[quest_id]["quest"] = True
            # only passing how much we actually need to do
            self.quest_self_doable[quest_id]["till"] = quest["total"] - quest["current"]

        # Handle quests: one independent check per quest, one after another (B2, not an if/elif chain)
        for quest in quests.copy():
            if self._should_stop():
                return
            quest_id = quest["quest_id"]
            quest_till = self.quest_self_doable[quest_id]["till"]
            # 1. hunt, and find_animal_<rank> (counted from the own hunt results, B5)
            if quest_id == "hunt" and not self.get_cmd_cnf("hunt").enabled and self.settings.canEnable:
                await self.bot.log(
                    f"Attempting to do quest - running hunt x{quest_till}",
                    color="#a5c7e3",
                )
                await self._run_self_quest("hunt", "hunt", till=quest_till)

            if quest_id.startswith("find_animal_") and not self.get_cmd_cnf("hunt").enabled and self.settings.canEnable:
                self.catcha_a_rank = quest_id.replace("find_animal_", "")
                await self.bot.log(
                    f"Attempting to do quest - catch pet of {self.catcha_a_rank} (hunt)",
                    color="#a5c7e3",
                )
                await self._run_self_quest(quest_id, "hunt")

            # 2. battle_xp and battle count the own battle results (B4, B5)
            if quest_id == "battle_xp" and not self.get_cmd_cnf("battle").enabled and self.settings.canEnable:
                await self.bot.log(
                    f"Attempting to do quest - gain {quest_till} xp from battle",
                    color="#a5c7e3",
                )
                await self._run_self_quest("battle_xp", "battle")

            if quest_id == "battle" and not self.get_cmd_cnf("battle").enabled and self.settings.canEnable:
                await self.bot.log(
                    f"Attempting to do quest - running battle x{quest_till}",
                    color="#a5c7e3",
                )
                await self._run_self_quest("battle", "battle")

            # 3. owo
            if quest_id == "owo" and not self.get_cmd_cnf("owo").enabled and self.settings.canEnable:
                await self.bot.log(
                    f"Attempting to do quest - send owo x{quest_till}",
                    color="#a5c7e3",
                )
                await self._run_self_quest("owo", "owo", till=quest_till)

            # 4. gamble
            if quest_id == "gamble" and not self.is_gamble_enabled and self.settings.canEnable:
                await self.bot.log(
                    f"Attempting to do quest - running gamble commands x{quest_till}",
                    color="#a5c7e3",
                )
                await self._run_self_quest(
                    "gamble", self.bot.random.choice(["slots", "coinflip"]), till=quest_till
                )

            # 5. action_send: needs a partner account; enableCommandsToCompleteQuest is checked inside (B2)
            if quest_id == "action_send":
                await self.handle_action_quest(quest_till)

    async def handle_helpable_quests(self, quests):
        """Quests others can't do themselves, Blocking.."""
        """
        Quests Format:

        data = {
            "userid": userid,
            "current": quest_detail["current"],
            "total": quest_detail["total"],
            "complete": quest_detail["complete"],
            "claim_userid": None,
            "quest_id": quest_id,
            "channel_id": channel_id,
            "guild_id": guild_id,
        }
        """
        chnl, user = None, None
        if not quests:
            return

        await self.bot.log("Attempting to help other quests", color="#a5c7e3")

        pending_wait = False  # a battle help flag is on: the wait at the end covers it (C1)
        for quest in quests:
            if self._should_stop():
                break
            quest_id = quest["quest_id"]
            # Try see if user's channel is fetchable
            try:
                chnl = await self.bot.fetch_channel(quest["channel_id"])
            except Exception:
                chnl = None

            # see if user is in our guild
            try:
                user = await self.bot.cm.guild.fetch_member_profile(quest["userid"])
            except Exception:
                user = None

            if not (chnl or user) or not _in_guild(self.bot, quest.get("guild_id")):
                # C8: the owner's channel cannot be seen from this account: no partner in this server
                await self.bot.log("[quest] no partner in this server", color="#e6e65a")
                continue
            else:
                if quest_id not in {"cookie", "pray", "curse"}:
                    success = await self.bot.quest_handler.qh.claim_quest(
                        quest["userid"], quest_id, self.bot.user.id
                    )
                else:
                    success = True

            # Handle quest
            if success:
                till = quest["total"] - quest["current"]
                channel = chnl.id if chnl else None
                if quest_id == "action_receive":
                    # the quest owner is the target, never a hard-coded id. The claim goes back when the help is over (C6)
                    try:
                        await self.handle_action_quest(till=till, userid=quest["userid"])
                    finally:
                        await self.bot.quest_handler.qh.release_claim(
                            quest["userid"], quest_id, self.bot.user.id
                        )

                elif quest_id == "battle_friend":
                    await self.bot.log(
                        f"Attempting to help {quest['userid']} with {quest_id}",
                        color="#a5c7e3",
                    )
                    if (
                        not self.get_cmd_cnf("battle").enabled
                        and self.settings.canEnable
                    ):
                        try:
                            # C7: progress counts the echoed battle commands (see handle_repeat_quest)
                            await self.handle_repeat_quest(
                                "battle",
                                till,
                                f"<@{quest['userid']}>",
                                quest["userid"],
                                "battle_friend",
                                None,
                                confirmed=True,
                            )
                        finally:
                            await self.bot.quest_handler.qh.release_claim(
                                quest["userid"], quest_id, self.bot.user.id
                            )
                    else:
                        self.bot.quest_help_request["battle"]["channel"] = channel
                        self.bot.quest_help_request["battle"]["till"] = till
                        self.bot.quest_help_request["battle"]["userid"] = quest[
                            "userid"
                        ]
                        self.bot.quest_help_request["battle"]["enabled"] = True
                        # the battle cog runs this help: the wait at the end covers it (C1)
                        pending_wait = True

                elif quest_id in {"cookie", "pray", "curse"}:
                    # C1: nothing reads the cookie flag, so cookie help is always sent from here. pray and curse share
                    # their cooldown: while either command runs, its loop sends the help through the flag
                    enabled = False
                    if quest_id != "cookie":
                        enabled = (
                            self.get_cmd_cnf("pray").enabled
                            or self.get_cmd_cnf("curse").enabled
                        )
                    await self.bot.log(
                        f"Attempting to help {quest['userid']} with {quest_id}",
                        color="#a5c7e3",
                    )
                    if not enabled and self.settings.canEnable:
                        # if not chnl, it will be same guild. we may send in our own channel if same guild
                        await self.handle_repeat_quest(
                            quest_id,
                            till,
                            f"<@{quest['userid']}>",
                            quest["userid"],
                            quest_id,
                            channel,
                            confirmed=True,
                        )
                    elif quest_id != "cookie":
                        self.bot.quest_help_request[quest_id]["channel"] = channel
                        self.bot.quest_help_request[quest_id]["till"] = till
                        self.bot.quest_help_request[quest_id]["userid"] = quest[
                            "userid"
                        ]
                        self.bot.quest_help_request[quest_id]["enabled"] = True
                    await self.wait_till_unclaimable_quest_done()
                    pending_wait = False

        if pending_wait:
            # the battle help is run by the battle cog: wait for it too (C1). This also releases the claims (C6)
            await self.wait_till_unclaimable_quest_done()

    async def wait_till_unclaimable_quest_done(self):
        """
        Waits until the helped quests (cookie, pray, curse, battle) are done by the cogs that run them (C1).
        Bounded by QUEST_HELP_WAIT_MAX_SECONDS and by a stop. Whatever ends the wait, the help flags of this account
        are reset and its claims go back to the list (C1, C6)
        """
        qsts = ["cookie", "curse", "pray", "battle"]
        started = _monotonic()
        try:
            while True:
                if self._should_stop():
                    break
                active = [qst for qst in qsts if self.bot.quest_help_request[qst]["enabled"]]
                if not active:
                    break
                waited = _monotonic() - started
                if waited >= QUEST_HELP_WAIT_MAX_SECONDS:
                    await self.bot.log(
                        f"[quest] help wait timeout {', '.join(active)}",
                        color="#e6e65a",
                    )
                    break
                await asyncio.sleep(min(1.0, QUEST_HELP_WAIT_MAX_SECONDS - waited))
        finally:
            for qst in qsts:
                self.bot.quest_help_request[qst] = {
                    "till": 0,
                    "enabled": False,
                    "userid": 0,
                    "channel": 0,
                }
            await self.bot.quest_handler.qh.release_claims_by(self.bot.user.id)

    async def handle_action_quest(self, till: int, userid=None, channelid=None):
        """
        Sends `till` random actions.
        - helping (action_receive): `userid` is the quest owner. The target is that owner and the progress is updated.
        - own quest (action_send): no `userid`. The target is another account running in this process.
        There is never a hard-coded target: with no valid one, the quest is skipped.
        """
        if not userid:
            # the own action_send quest (B2, B3)
            await self._handle_own_action_quest(till, channelid)
            return
        target = userid

        actions = ["stare", "kill", "greet", "punch", "wave", "slap"]
        arg = f"<@{target}>"
        try:
            for _ in range(int(till or 0)):
                if self._should_stop():
                    break
                action = self.bot.random.choice(actions)
                cmd = {
                    "cmd_name": action,
                    "cmd_arguments": arg,
                    "prefix": True,
                    "checks": True,
                    "id": "action",
                    "channel": channelid,
                }
                if not await self._quest_send(cmd):
                    break
                # our own message shows it was sent: its check entry is dropped, see handle_repeat_quest
                await self.block_till_send(action, channelid, True, arg)
                await self.bot.ch.remove_queue(id="action")
                await self.bot.sleep_till([4, 6])
                if userid:
                    # update quest progress
                    current, completed = await self.bot.quest_handler.qh.update_progress(
                        self.bot.user.id, userid, "action_receive"
                    )
                    if current is not None:
                        await self.bot.quest_handler.sync_progress(
                            "action_receive", current, completed
                        )
                    if completed:
                        break
        finally:
            # no cog reads action replies: an action check entry must never stay behind for monitor_checks
            await self.bot.ch.remove_queue(id="action")

    def get_cmd_cnf(self, key: str):
        base_cnf = self.bot.settings_dict.commands
        if key in {"slots", "coinflip"}:
            base_cnf = self.bot.settings_dict.gamble
        return getattr(base_cnf, key)

    def check(self, message, content, channel_id=None):
        return (
            message.author.id == self.bot.user.id
            and message.channel.id == (channel_id if channel_id else self.bot.cm.id)
            and message.content == content
        )

    async def block_till_send(self, content, channel_id=None, prefix=False, arg=None) -> bool:
        """True when our own message with this content showed up, False after 30 s (B3 counts only True)"""
        if prefix:
            content = f"{self.bot.settings_dict.prefix}{content}"
        if arg:
            content += f" {arg}"
        try:
            await self.bot.wait_for(
                "message",
                check=lambda m: self.check(m, content, channel_id),
                timeout=30.0,
            )
            return True
        except asyncio.TimeoutError:
            print("Block till send got timeout error, likely network issue.")
            return False

    async def handle_repeat_quest(
        self,
        cmd_name,
        till: int = None,
        arg: str = None,
        upd_userid: int = None,
        upd_questid: str = None,
        chn_to_send: int = None,
        confirmed: bool = False,
    ) -> bool:
        """
        Repeat quest handles repeated sending of commands
        The end of this loop is either determined by `till` if provided
        or the external flag `self.repeat_quest_flag` being set to true

        Just incase when dashboard gets added, and user manually enables the disabled command, we
        exit partially
        """

        user_till_value = True if till else False
        queue_id = cmd_name if cmd_name != "curse" else "pray"
        if cmd_name == "hunt" and self.catcha_a_rank and not till:
            label = f"find_animal_{self.catcha_a_rank}"
        elif cmd_name == "battle" and not till:
            label = "battle_xp"
        else:
            label = upd_questid or cmd_name
        started = _monotonic()
        sent = 0
        misses = 0  # confirmed runs only (C7)

        try:
            while True:
                cmd = {
                    "cmd_name": cmd_name,
                    "cmd_arguments": "1" if cmd_name in {"slots", "coinflip"} else arg,
                    "prefix": True if cmd_name != "owo" else False,
                    "checks": True,
                    "id": queue_id,
                    "channel": chn_to_send,
                }
                if self._should_stop():
                    break
                cnf = self.get_cmd_cnf(cmd_name)

                if cnf.enabled:
                    return False

                else:
                    if self.repeat_quest_flag:
                        self.repeat_quest_flag = False
                        break

                # Limits: a run without a known count stops after QUEST_REPEAT_MAX_COMMANDS commands,
                # every run stops after QUEST_REPEAT_MAX_SECONDS
                if _monotonic() - started >= QUEST_REPEAT_MAX_SECONDS or (
                    not user_till_value and sent >= QUEST_REPEAT_MAX_COMMANDS
                ):
                    await self.bot.log(
                        f"[quest] {label} repeat limit reached, will re-check quest log",
                        color="#e6e65a",
                    )
                    break

                if confirmed:
                    # C7: a help command counts only when its echo was seen. Three in a row without echo end the run
                    outcome = await self._send_and_confirm(
                        cmd_name,
                        arg=cmd["cmd_arguments"],
                        prefix=cmd["prefix"],
                        channel=chn_to_send,
                        queue_id=queue_id,
                    )
                    if outcome == "stopped":
                        break
                    sent += 1
                    if outcome != "echo":
                        misses += 1
                        if misses >= QUEST_ECHO_MISSES_STOP:
                            await self.bot.log(
                                f"[quest] {label} {QUEST_ECHO_MISSES_STOP} commands without echo, stopping run",
                                color="#e6e65a",
                            )
                            break
                        if not await self._sleep_checked(cnf.get_cd()):
                            break
                        continue
                    misses = 0
                else:
                    if not await self._quest_send(cmd):
                        break
                    sent += 1
                    # Our own message shows the command was sent. Its check entry is dropped right here,
                    # so monitor_checks cannot queue the command again (it re-queues entries that get no send for a while).
                    # The rest - battle xp and the hunt rank one, will be updated from on_message
                    await self.block_till_send(
                        cmd_name, chn_to_send, cmd["prefix"], cmd["cmd_arguments"]
                    )
                    await self.bot.ch.remove_queue(id=queue_id)

                if upd_questid and upd_userid:
                    current, completed = await self.bot.quest_handler.qh.update_progress(
                        self.bot.user.id, upd_userid, upd_questid
                    )
                    if current is not None:
                        await self.bot.quest_handler.sync_progress(
                            upd_questid, current, completed
                        )
                    if completed:
                        break

                if user_till_value:
                    till -= 1
                    if till <= 0:
                        break

                if not await self._sleep_checked(cnf.get_cd()):
                    break
        finally:
            # the run is over (end, limit, stop or error): nothing of it may be queued again later
            self.repeat_quest_flag = False  # B6
            self.catcha_a_rank = ""
            await self.bot.ch.remove_queue(id=queue_id)

        return True

    @tasks.loop()
    async def main_quest_loop(self):
        """One check per iteration, see _quest_loop_once. An unexpected error is logged and the loop goes on"""
        try:
            await self._quest_loop_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._log_unexpected("main_quest_loop")
            await asyncio.sleep(QUEST_ERROR_BACKOFF_SECONDS)

    async def _quest_loop_once(self):
        """
        Initially all accounts will do their own quests
        before starting to help others..
        There should be sufficient time in-between next quest for this

        Safety (phase A): nothing is sent before the next-quest time after "all quests done",
        "owo quest" is never sent sooner than QUEST_MIN_CHECK_SECONDS after the previous one,
        and the loop ends once the account stops.
        """
        if self._should_stop():
            self.main_quest_loop.cancel()
            return

        # 1. All quests done for today: nothing is sent until the next-quest time
        if self.all_quests_done:
            wait = self.bot.quest_handler.next_quest_timestamp - _wall_now()
            if wait > 0:
                await self.bot.log(
                    f"[quest] all quests done, next check in {int(wait)}s",
                    color="#a5c7e3",
                )
                if not await self._sleep_checked(wait):
                    return
            print(f"retiring {self.bot.user.name}")
            self.all_quests_done = False
            self.alr_posted = False
            # D4: every account wakes at the same reset time, so the first check after the wait is spread too
            self._spread_pending = True

        # 2. checkCooldown, and never sooner than QUEST_MIN_CHECK_SECONDS after the last check
        if not await self._sleep_checked(self.settings.get_cd()):
            return
        # D4: the first check after start (and after an all-done wait) is spread over QUEST_FIRST_CHECK_SPREAD_SECONDS
        if self._spread_pending:
            self._spread_pending = False
            if not await self._sleep_checked(self.bot.random.uniform(0.0, QUEST_FIRST_CHECK_SPREAD_SECONDS)):
                return
        if self._last_quest_check is not None:
            remainder = QUEST_MIN_CHECK_SECONDS - (_monotonic() - self._last_quest_check)
            if remainder > 0 and not await self._sleep_checked(remainder):
                return

        # 3. Put queue to quest command. Its check entry is dropped once our own message shows up
        self._last_quest_check = _monotonic()
        self._quest_log_event.clear()
        if not await self._quest_send(self.quest_cmd):
            return
        await self.block_till_send(self.quest_cmd["cmd_name"], None, True)
        await self.bot.ch.remove_queue(id="quest")
        await self.bot.sleep_till([5, 10])
        # B7: this round only works on a list that OwO has just sent for this account
        if not await self._wait_for_quest_log():
            if not self._should_stop():
                await self.bot.log(
                    "[quest] no quest log reply, skipping this round", color="#e6e65a"
                )
            return

        # 4. if help required, post
        if self.bot.quest_handler.help_required() and self.settings.useHelpChannel:
            if not self.alr_posted:
                await self._quest_send(self.get_quest_cmd(self.settings.channelId))
                self.alr_posted = True

        # 5. If self doable quests, then do them!
        self_doable = self.bot.quest_handler.get_self_doable_quests()
        await self.handle_self_quests(self_doable)

        # 6. Help other quest users
        if self.settings.helpOthers:
            helpable = self.bot.quest_handler.get_helpable_quests(
                claim_ttl=QUEST_CLAIM_TTL_SECONDS
            )
            await self.handle_helpable_quests(helpable)

    def enable_state_store(self, path=None):
        """D5: keeps the quest state of this account. The real start calls it from cog_load; tests pass a temp path"""
        self.store = QuestStore(path)

    async def _store_call(self, method, *args):
        """D5: one store call, off the event loop. A failure is logged once and never raised: the bot goes on without it"""
        try:
            return await asyncio.to_thread(getattr(self.store, method), *args)
        except Exception as error:
            if not self._store_failed:
                self._store_failed = True
                await self.bot.log(
                    f"[quest] quest state store failed ({type(error).__name__}), the quest state is not kept",
                    color="#e6e65a",
                )
            return None

    async def _persist_state(self):
        """D5: writes the quest state of this account. Does nothing when the state is not kept (no store)"""
        if self.store is None:
            return
        handler = self.bot.quest_handler
        values = {
            "next_quest_at": handler.next_quest_timestamp or None,
            "all_done": int(bool(self.all_quests_done)),
            "seals": handler.seals,
            "quests_json": json.dumps(handler.quests, ensure_ascii=False),
            "last_log_at": self._last_log_at,
            "last_ocr_ok_at": self._last_ocr_ok_at,
            "last_claim_at": self._last_claim_at,
            "claims_total": self._claims_total,
            "ocr_next_at": self._last_ocr_next_at,
        }
        await self._store_call("save", str(self.bot.user.id), values)

    async def restore_quest_state(self):
        """D5: reads this account's row when the cog starts. A state whose next-quest time is still ahead is restored:
        all done means the loop waits for that time; an open list is restored and still checked again. A row whose
        next-quest time has passed (or none) starts normally. Never raises"""
        if self.store is None:
            return
        row = await self._store_call("load", str(self.bot.user.id))
        if not row:
            return
        self._claims_total = row.get("claims_total") or 0
        self._last_claim_at = row.get("last_claim_at")
        next_at = row.get("next_quest_at")
        now = _wall_now()
        if not next_at or next_at <= now:
            return  # the quest log was reset since the last run: a normal start
        handler = self.bot.quest_handler
        handler.update_next_timestamp(next_at)
        handler.seals = row.get("seals")
        if row.get("all_done"):
            self.all_quests_done = True
            await self.bot.log(
                "[quest] all quests were done before the restart, waiting for the next-quest time",
                color="#a5c7e3",
            )
            return
        try:
            quests = json.loads(row.get("quests_json") or "[]")
        except (TypeError, ValueError):
            quests = []
        quests = [quest for quest in quests if isinstance(quest, dict) and "quest_id" in quest]
        handler.restore_quests(quests)
        self._last_log_at = row.get("last_log_at")
        self._last_ocr_ok_at = row.get("last_ocr_ok_at")
        self._last_ocr_next_at = row.get("ocr_next_at")
        if self._last_log_at:
            # the last Quest Log was less than QUEST_MIN_CHECK_SECONDS ago: the first check waits for the rest of it
            self._last_quest_check = _monotonic() - max(0.0, now - self._last_log_at)

    def _ocr_reusable(self, quest_details) -> bool:
        """D3: the last good read is used again when it belongs to the same next-quest time, is less than
        QUEST_OCR_REUSE_SECONDS old, and nothing can be claimed. Progress is counted locally since phase B"""
        next_at = quest_details["next_quest_timestamp"]
        if not next_at or quest_details["claimBtn"] or self._last_ocr_ok_at is None:
            return False
        if self._last_ocr_next_at != next_at:
            return False
        return 0 <= _wall_now() - self._last_ocr_ok_at < QUEST_OCR_REUSE_SECONDS

    async def on_component_message(self, message):
        try:
            await self._handle_component_message(message)
        except Exception:
            await self._log_unexpected("on_component_message")

    async def _handle_component_message(self, message):
        if message.author.id != self.bot.owo_bot_id:
            return

        if message.channel_id != self.bot.cm.id:
            return

        components, buttons = message.components, message.buttons
        texts = _all_texts(message)  # B9: every text of the message, not only the top level
        if not self.is_valid_quest(components):
            if any(QUEST_ALL_DONE_TEXT in text for text in texts):
                self._dump_quest_debug(texts, components, None)  # B10
            return

        quest_details = None
        try:
            quest_details = await self._apply_quest_message(
                message, components, buttons, texts
            )
        finally:
            self._dump_quest_debug(texts, components, quest_details)  # B10
        # B7: reached only when the message was processed without an error: the round may use the list now
        self._quest_log_event.set()
        self._last_log_at = _wall_now()  # D5: the quest state is written after each parsed Quest Log
        await self._persist_state()

    async def _apply_quest_message(self, message, components, buttons, texts):
        """The quest log work for one message. Returns the parsed details"""
        # Remove quest command that was queued
        await self.bot.ch.remove_queue(id="quest")
        # Fetch quest details
        quest_details = self.get_available_quest_details(
            components=components, buttons=buttons, texts=texts
        )
        if quest_details["seals"] is not None:
            self.bot.quest_handler.seals = quest_details["seals"]  # D6
        # The quest would get edited, sure but
        # the image we fetched earlier wouldn't change
        quest_channel = await self.bot.fetch_channel(message.channel_id)

        if quest_details["claimBtn"]:
            await quest_details["claimBtn"].click(
                self.bot.ws.session_id,
                self.bot.local_headers,
                quest_channel.guild.id,
            )
            await self.bot.log("Claimed a finished quest!", color="#a5c7e3")
            self._last_claim_at = _wall_now()  # D5, D7
            self._claims_total += 1

        if quest_details["questDone"]:
            await self.bot.log(
                "User has finished all of their quests!", color="#a5c7e3"
            )
            self.all_quests_done = True
            # nothing is left to do today: the helper registrations of this account go too (C5)
            await self.bot.quest_handler.clear_quests()
            # The next-quest time comes from this same message. It was never stored before (8.7)
            next_quest_timestamp = quest_details["next_quest_timestamp"]
            if not next_quest_timestamp:
                next_quest_timestamp = int(_wall_now()) + QUEST_DONE_FALLBACK_SECONDS
                await self.bot.log(
                    "[quest] all done, no next-quest time found, waiting 4h",
                    color="#e6e65a",
                )
            self.bot.quest_handler.update_next_timestamp(next_quest_timestamp)
            return quest_details

        # Fetch quests from the fetched image
        # and Update the fetched quest to quest_handler

        # Whether http or https, both must be in url. otherwise invalid.
        url = quest_details["url"]
        if not isinstance(url, str) or "http" not in url:
            await self.bot.log("[quest] no quest image in message", color="#e6e65a")
            return quest_details

        # D3: no OCR when the last good read belongs to this quest log, is recent, and nothing can be claimed
        if self._ocr_reusable(quest_details):
            self.bot.quest_handler.update_next_timestamp(quest_details["next_quest_timestamp"])
            return quest_details

        await self.bot.log("Trying to update quest lists...", color="#a5c7e3")
        updated = await self.bot.quest_handler.update_quests(
            url,
            self.bot.cm.id,
            self.bot.cm.guild.id,
            quest_details["next_quest_timestamp"],
        )
        if updated:
            self._last_ocr_ok_at = _wall_now()
            self._last_ocr_next_at = quest_details["next_quest_timestamp"] or None
        if not updated:
            await self.bot.log(
                "[quest] OCR failed, keeping previous quest list", color="#e6e65a"
            )
        return quest_details

    @commands.Cog.listener()
    async def on_message(self, message):
        try:
            await self._handle_message(message)
        except Exception:
            await self._log_unexpected("on_message")

    async def _handle_message(self, message):
        if not (
            message.channel.id == self.bot.cm.id
            and message.author.id == self.bot.owo_bot_id
        ):
            return

        # find_animal_<rank>: each own match counts 1 for its rank (B5). The "hunt" quest key is not needed
        lowered = message.content.lower()
        if ("you found:" in lowered or "caught" in lowered) and self.bot.is_for_us(message):
            for rank in animal_ranks:
                key = f"find_animal_{rank}"
                if self.quest_self_doable[key]["quest"] and has_ranked_emoji(
                    self, message.content, rank
                ):
                    self.quest_self_doable[key]["till"] -= 1
                    if self.quest_self_doable[key]["till"] <= 0 and self._active_run == key:
                        self.repeat_quest_flag = True

        # battle and battle_xp (B4, B5): one own battle embed is one battle, its footer gives the XP
        battle_q = self.quest_self_doable["battle"]
        xp_q = self.quest_self_doable["battle_xp"]
        if battle_q["quest"] or xp_q["quest"]:
            for embed in message.embeds or []:
                author = getattr(embed, "author", None)
                if getattr(author, "name", None) is None or not self.bot.name_phrase_in(
                    author.name, "{name} goes into battle!", message
                ):
                    continue
                xp = 0
                footer = getattr(embed, "footer", None)
                found = re.search(r"\+([\d,]+) xp", getattr(footer, "text", None) or "")
                if found:
                    xp = int(found.group(1).replace(",", ""))
                if battle_q["quest"]:
                    battle_q["till"] -= 1
                    if battle_q["till"] <= 0 and self._active_run == "battle":
                        self.repeat_quest_flag = True
                if xp_q["quest"] and xp:
                    xp_q["till"] -= xp
                    if xp_q["till"] <= 0 and self._active_run == "battle_xp":
                        self.repeat_quest_flag = True


async def setup(bot):
    await bot.add_cog(Quest(bot))
