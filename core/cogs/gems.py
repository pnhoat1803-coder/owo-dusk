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
import re
import time
import traceback
from collections import deque
from types import SimpleNamespace

from discord.ext import commands
from discord.ext.commands import ExtensionNotLoaded

from core.cogs._BASE import BaseCog
from utils.lootbox import select_lootbox_action

"""
REF :-
gem code - https://github.com/ChristopherBThai/Discord-OwO-Bot/blob/master/src/commands/commandList/shop/inventory.js
small numbers - https://github.com/ChristopherBThai/Discord-OwO-Bot/blob/master/src/commands/commandList/shop/util/shopUtil.js
"""


gem_tiers = {
    "common": ["051", "065", "072", "079"],
    "uncommon": ["052", "066", "073", "080"],
    "rare": ["053", "067", "074", "081"],
    "epic": ["054", "068", "075", "082"],
    "mythical": ["055", "069", "076", "083"],
    "legendary": ["056", "070", "077", "084"],
    "fabled": ["057", "071", "078", "085"],
}

# Gem kinds, in the order of the ids inside each `gem_tiers` row
KIND_ORDER = ("huntGem", "empoweredGem", "luckyGem", "specialGem")
KIND_LABELS = {
    "huntGem": "hunting",
    "empoweredGem": "empowering",
    "luckyGem": "lucky",
    "specialGem": "special",
}
TIER_ORDER = ("common", "uncommon", "rare", "epic", "mythical", "legendary", "fabled")
SPECIAL_GEM_IDS = {row[3] for row in gem_tiers.values()}

# Sends stay paused this long while waiting for the inventory reply, then resume on their own
INVENTORY_REPLY_TIMEOUT = 60
# After this long with no gems found, inventory checks are allowed again on the next catch
NO_GEM_RECHECK_SECONDS = 30 * 60
# Special gem uses are counted over this window, see Gems.special_cap_blocks
SPECIAL_CAP_WINDOW = 60 * 60
# How long the "used gems" line waits for owouse to be sent. A send held by a captcha or a pause can take a while
GEM_SEND_LOG_TIMEOUT = 300


def convert_small_numbers(small_number):
    numbers = {
        "⁰": "0",
        "¹": "1",
        "²": "2",
        "³": "3",
        "⁴": "4",
        "⁵": "5",
        "⁶": "6",
        "⁷": "7",
        "⁸": "8",
        "⁹": "9",
    }
    normal_string = "".join(numbers.get(char, char) for char in small_number)
    return int(normal_string)


def find_gems_available(message):
    available_gems = {
        "fabled": {"057": 0, "071": 0, "078": 0, "085": 0},  # fabled
        "legendary": {"056": 0, "070": 0, "077": 0, "084": 0},  # legendary
        "mythical": {"055": 0, "069": 0, "076": 0, "083": 0},  # mythical
        "epic": {"054": 0, "068": 0, "075": 0, "082": 0},  # epic
        "rare": {"053": 0, "067": 0, "074": 0, "081": 0},  # rare
        "uncommon": {"052": 0, "066": 0, "073": 0, "080": 0},  # uncommon
        "common": {"051": 0, "065": 0, "072": 0, "079": 0},  # common
        # hunt, emp, luck, special
    }
    """
    Example output:-
    [('050', '⁰⁷'), ('051', '⁰³')]
    """
    inv_numbers = re.findall(r"`(\d+)`[^`\n]*?([⁰¹²³⁴⁵⁶⁷⁸⁹]+)", message)
    for gem_id, small_number in inv_numbers:
        gem_count = convert_small_numbers(small_number)

        for _, gems in available_gems.items():
            if gem_id in gems:
                gems[gem_id] = gem_count
                break
    return available_gems


def find_box_items_available(message):
    available_items = {"049": 0, "050": 0, "099": 0, "100": 0}
    inv_numbers = re.findall(r"`(049|050|099|100)`[^`\n]*?([⁰¹²³⁴⁵⁶⁷⁸⁹]+)", message)
    for item_id, small_number in inv_numbers:
        available_items[item_id] = convert_small_numbers(small_number)
    return available_items


def component_texts(components):
    """Text displays of a message's components, including the ones inside sections"""
    texts = []
    for component in components or []:
        name = getattr(component, "component_name", None)
        if name == "text_display" and component.content:
            texts.append(component.content)
        elif name == "section":
            texts.extend(component_texts(component.components))
    return texts


def message_text(message):
    """The message content, followed by the text of its components"""
    parts = [message.content or ""]
    parts += component_texts(getattr(message, "components", None))
    return "\n".join(parts)


def is_inventory_text(text):
    return "'s Inventory" in text


def make_fabled_lootbox_commands(count, settings):
    if count <= 0 or not settings.fabledLootbox:
        return []
    return [
        {
            "cmd_name": settings.fabledLootboxCommand,
            "cmd_arguments": "",
            "prefix": True,
            "checks": False,
            "id": "fabledLootbox",
        }
        for _ in range(count)
    ]


def make_inventory_box_commands(available_items, settings, aliases):
    commands = make_fabled_lootbox_commands(
        available_items.get("049", 0), settings
    )
    for item_code in ("050", "100", "099"):
        if available_items.get(item_code, 0) <= 0:
            continue
        action = select_lootbox_action(item_code, settings)
        if action is None:
            continue
        command_id, arguments = action
        commands.append(
            {
                "cmd_name": aliases[command_id]["normal"],
                "cmd_arguments": arguments,
                "prefix": True,
                "checks": False,
                "id": command_id,
            }
        )
    return commands


def has_known_gems(available_gems):
    return any(
        count > 0
        for gems in available_gems.values()
        for count in gems.values()
    )


class Gems(BaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.grouped_gems = None
        self.available_gems = {}
        self.available_box_items = {}
        # An inventory request is out and its reply is still expected
        self.inventory_check = False
        # Gem kinds that request is for, None means every enabled kind
        self.pending_kinds = None
        self.read_inventory_ids = deque(maxlen=50)
        # Per gem kind: the inventory is not asked for that kind again before this time
        self.kind_retry_at = {}
        # Gem decisions wait until this time, then the next hunt reply confirms the state
        self.gem_wait_until = None
        self.recent_special_gem_usage = 0
        # Hunt replies since each gem kind was last used, None until it is used (see guard_blocks)
        self.hunts_since_use = dict.fromkeys(KIND_ORDER)
        # The kinds the reuse guard has logged for, once per account per kind
        self.guard_logged = set()
        # Monotonic times of the special gem uses in the rolling hour (see special_cap_blocks)
        self.special_uses = deque()
        self.special_cap_logged = False
        self.no_gem_at = 0
        self.last_inventory_check_at = None
        self.last_no_gems_log_at = None
        self.gem_cmd = {
            "cmd_name": self.bot.alias["use"]["normal"],
            "cmd_arguments": "",
            "prefix": True,
            "checks": False,
            "id": "gems",
        }
        # Not in `checks`: a reply that never comes must not make the inventory get sent again
        self.inv_cmd = {
            "cmd_name": self.bot.alias["inv"]["normal"],
            "prefix": True,
            "checks": False,
            "id": "inv",
        }

    @property
    def settings(self):
        return self.bot.settings_dict.autoUse.gems

    def inventory_request_allowed(self, now=None):
        now = time.monotonic() if now is None else now
        if self.last_inventory_check_at is None:
            return True
        cooldown = float(
            self.bot.settings_dict.autoUse.inventoryCheckCooldown
        )
        return now - self.last_inventory_check_at >= cooldown

    def inventory_request_in_flight(self):
        return (
            self.inventory_check
            and time.monotonic() - self.last_inventory_check_at
            < INVENTORY_REPLY_TIMEOUT
        )

    def kind_check_allowed(self, kind):
        return time.monotonic() >= self.kind_retry_at.get(kind, 0)

    def note_inventory_use(self):
        """Gems were used or a box opened, so the next gem decision waits for a hunt reply after this delay"""
        delay = float(
            self.bot.settings_dict.autoUse.inventoryRecheckAfterUse
        )
        self.gem_wait_until = time.monotonic() + delay

    async def request_inventory(self, kinds=None, priority=True):
        now = time.monotonic()
        if not self.inventory_request_allowed(now):
            return False
        self.last_inventory_check_at = now
        self.inventory_check = True
        self.pending_kinds = kinds
        await self.bot.ch.pause_with_timeout(INVENTORY_REPLY_TIMEOUT)
        await self.bot.ch.put_queue(self.inv_cmd, priority=priority)
        return True

    async def log_no_usable_gems(self, reason):
        now = time.monotonic()
        if (
            self.last_no_gems_log_at is not None
            and now - self.last_no_gems_log_at < 10 * 60
        ):
            return
        self.last_no_gems_log_at = now
        account_name = getattr(self.bot, "username", "account")
        await self.bot.log(
            f"{account_name}: no usable gems: {reason}", "#924444"
        )

    def gem_check_allowed(self):
        """After no gems were found, inventory checks stay off until NO_GEM_RECHECK_SECONDS have passed and a catch comes in"""
        if not self.bot.stats.no_gem:
            return True
        if time.monotonic() - self.no_gem_at < NO_GEM_RECHECK_SECONDS:
            return False
        self.bot.stats.no_gem = False
        return True

    def should_use_special_gem(self):
        if not self.settings.gemsToUse["specialGem"]:
            return False
        if self.settings.dynamicSpecialGem and not self.bot.ongoing_owobot_event:
            return False
        return True

    def is_special_gem_failure(self):
        if not self.settings.dynamicSpecialGem:
            return False
        if self.recent_special_gem_usage == 0:
            return False
        usage_time = time.monotonic() - self.recent_special_gem_usage
        return usage_time < 60

    def enabled_gem_types(self):
        cnf = self.settings.gemsToUse
        return {
            "huntGem": cnf["huntGem"],
            "empoweredGem": cnf["empoweredGem"],
            "luckyGem": cnf["luckyGem"],
            "specialGem": self.should_use_special_gem(),
        }

    def pick_gem_ids(self, available_gems, kinds):
        """One gem id per kind: the first tier, in the configured order, that has a gem in the inventory"""
        enabled = self.enabled_gem_types()
        tier_order = TIER_ORDER if self.settings.useLowest else TIER_ORDER[::-1]
        picks = {}
        for kind in KIND_ORDER:
            if kind not in kinds or not enabled.get(kind):
                continue
            index = KIND_ORDER.index(kind)
            for tier in tier_order:
                if not self.settings.tiers.get(tier):
                    continue
                gem_id = gem_tiers[tier][index]
                if available_gems.get(tier, {}).get(gem_id, 0) > 0:
                    picks[kind] = gem_id
                    break
        return picks

    async def send_gem_use(self, picks):
        gem_ids = [picks[kind] for kind in KIND_ORDER if kind in picks]
        gem_cmd = dict(self.gem_cmd, cmd_arguments=" ".join(gem_ids))
        sent = self.bot.ch.new_sent_event(gem_cmd)
        await self.bot.ch.put_queue(gem_cmd, priority=True)
        now = time.monotonic()
        self.reduce_used_gems(gem_ids)
        if self.bot.hunt_disabled:
            self.bot.hunt_disabled = False
        self.note_inventory_use()
        for kind in picks:
            self.hunts_since_use[kind] = 0
        if any(gem_id in SPECIAL_GEM_IDS for gem_id in gem_ids):
            self.recent_special_gem_usage = now
            self.special_uses.append(now)
        asyncio.create_task(self.log_gem_use_when_sent(sent, gem_ids))

    async def log_gem_use_when_sent(self, sent, gem_ids):
        """Logs the use once owouse was sent. A failed send, or a copy dropped from the queue, logs nothing"""
        try:
            await asyncio.wait_for(sent.wait(), timeout=GEM_SEND_LOG_TIMEOUT)
        except asyncio.TimeoutError:
            return
        if getattr(sent, "sent_message_id", None) is None:
            return
        account_name = getattr(self.bot, "username", "account")
        await self.bot.log(
            f"{account_name}: used gems: {' '.join(gem_ids)}", "#b168e6"
        )

    async def use_pending_gems(self, kinds):
        enabled = self.enabled_gem_types()
        kinds = [kind for kind in (kinds or KIND_ORDER) if enabled.get(kind)]
        kinds = await self.allowed_kinds(kinds)
        if not kinds:
            return
        picks = self.pick_gem_ids(self.available_gems, kinds)
        retry_at = time.monotonic() + float(
            self.bot.settings_dict.autoUse.inventoryCheckCooldown
        )
        skipped = [kind for kind in kinds if kind not in picks]
        for kind in skipped:
            self.kind_retry_at[kind] = retry_at
        if skipped:
            reasons = [f"no {KIND_LABELS[kind]} gem in inv" for kind in skipped]
            await self.log_no_usable_gems(", ".join(reasons))
        if picks:
            await self.send_gem_use(picks)
            return

        all_kinds = [kind for kind in KIND_ORDER if enabled.get(kind)]
        if not self.pick_gem_ids(self.available_gems, all_kinds):
            self.bot.stats.no_gem = True
            self.no_gem_at = time.monotonic()
            if not self.bot.hunt_disabled and self.settings.disableHuntIfNoGems:
                await self.bot.log(
                    "Disabling hunt since there is no gems to be used.", "#C51818"
                )
                """
                Currently no_gems status isn't being reset after being set
                """
                self.bot.hunt_disabled = True

    def missing_gem_kinds(self, msg):
        """Enabled gem kinds that the hunt message shows no active gem for"""
        gem_names = {
            "gem1": "huntGem",
            "gem4": "luckyGem",
            "gem3": "empoweredGem",
            "star": "specialGem",
        }
        tier_prefixes = ("c", "u", "r", "e", "m", "l", "f")

        gems_in_use = {
            kind
            for name, kind in gem_names.items()
            for prefix in tier_prefixes
            if f"{prefix}{name}" in msg
        }
        enabled = self.enabled_gem_types()
        return [
            kind for kind in KIND_ORDER
            if enabled[kind] and kind not in gems_in_use
        ]

    def count_hunt(self):
        for kind, hunts in self.hunts_since_use.items():
            if hunts is not None:
                self.hunts_since_use[kind] = hunts + 1

    async def allowed_kinds(self, kinds):
        """The kinds that may be used now. The reuse guard and the special cap can hold back a kind that the
        hunt message shows as missing, so the inventory is not asked for it either"""
        allowed = []
        for kind in kinds:
            if kind == "specialGem" and await self.special_cap_blocks():
                continue
            if await self.guard_blocks(kind):
                continue
            allowed.append(kind)
        return allowed

    async def guard_blocks(self, kind):
        """A kind used fewer than gems.minHuntsBeforeReuse hunts ago stays off, even when detection says it is missing"""
        hunts = self.hunts_since_use.get(kind)
        if hunts is None or hunts >= self.settings.minHuntsBeforeReuse:
            return False
        if kind not in self.guard_logged:
            self.guard_logged.add(kind)
            account_name = getattr(self.bot, "username", "account")
            await self.bot.log(
                f"{account_name}: gem guard: {KIND_LABELS[kind]} looks inactive after {hunts} hunts, skipping",
                "#924444",
            )
        return True

    async def special_cap_blocks(self):
        """At most gems.maxSpecialPerHour special gem uses in any rolling hour. 0 means never"""
        cap = self.settings.maxSpecialPerHour
        now = time.monotonic()
        while self.special_uses and now - self.special_uses[0] >= SPECIAL_CAP_WINDOW:
            self.special_uses.popleft()
        if len(self.special_uses) < cap:
            self.special_cap_logged = False
            return False
        if not self.special_cap_logged:
            self.special_cap_logged = True
            account_name = getattr(self.bot, "username", "account")
            await self.bot.log(
                f"{account_name}: special gems capped at {cap} per hour, skipping",
                "#924444",
            )
        return True

    def reduce_used_gems(self, used_gem_ids):
        for gem_id in used_gem_ids:
            for _, gems in self.available_gems.items():
                if gem_id in gems:
                    if gems[gem_id] > 0:
                        gems[gem_id] -= 1
                    if gems[gem_id] < 0:
                        # Huh?
                        gems[gem_id] = 0
                    break

    async def cog_load(self):
        if (
            not self.bot.settings_dict.commands.hunt.enabled
            or not self.settings.enabled
        ):
            try:
                asyncio.create_task(self.bot.unload_cog("core.cogs.gems"))
            except ExtensionNotLoaded:
                pass
            return
        self.bot.message_dispatcher.register(self.on_inventory_message)
        self.bot.message_dispatcher.register(self.on_inventory_message, edit=True)

    async def cog_unload(self):
        self.bot.message_dispatcher.unregister(self.on_inventory_message)
        self.bot.message_dispatcher.unregister(self.on_inventory_message, edit=True)
        await self.bot.ch.remove_queue(id="gems")

    @commands.Cog.listener()
    async def on_message(self, message):
        if (
            message.channel.id != self.bot.channel_id
            or message.author.id != self.bot.owo_bot_id
        ):
            return
        if not self.bot.is_for_us(message):
            return

        lowered = message.content.lower()
        opened_item = (
            "and finds" in lowered
            and ("opens a " in lowered or "<a:boxopen:" in lowered)
            and ("lootbox" in lowered or "crate" in lowered)
        )
        used_gem = "gem" in lowered and any(
            word in lowered for word in ("used", "activated", "equipped")
        )
        text = message_text(message)
        if "caught" in message.content or "hunt is empowered by" in message.content:
            # Checked first: a hunt reply must not count as a gem use and block its own decision
            await self.handle_hunt_reply(message)
            return

        if opened_item or used_gem:
            self.note_inventory_use()

        if is_inventory_text(text):
            await self.read_inventory_safely(message, text)
        elif "you already have an active Special gem" in message.content:
            if self.is_special_gem_failure():
                await self.bot.log(
                    "Warn: Special Gem being disabled, seems like the event ended",
                    "#924444",
                )
                self.bot.ongoing_owobot_event = False
                # Reset rest of the states:
                self.recent_special_gem_usage = 0

    async def on_inventory_message(self, message):
        """Inventory replies can arrive as component text, or as an edit of the first message"""
        if (
            message.channel_id != self.bot.channel_id
            or message.author.id != self.bot.owo_bot_id
        ):
            return
        text = message_text(message)
        if not is_inventory_text(text) or not self.inventory_is_ours(text):
            return
        # Reading can wait for the queue, so it runs apart from the other handlers of this message
        asyncio.create_task(self.read_inventory_safely(message, text))

    def inventory_is_ours(self, text):
        """Component text has no guild attached, so the account is looked for by mention and by name"""
        mentions = (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>")
        if any(mention in text for mention in mentions):
            return True
        guild = getattr(getattr(self.bot, "cm", None), "guild", None)
        return self.bot.name_in(text, SimpleNamespace(guild=guild))

    async def handle_hunt_reply(self, message):
        self.count_hunt()
        if not self.gem_check_allowed():
            return
        if self.inventory_request_in_flight():
            # The inventory reply decides for this request, so a hunt reply adds nothing yet
            return
        if self.gem_wait_until is not None:
            if time.monotonic() < self.gem_wait_until:
                return
            self.gem_wait_until = None

        missing = self.missing_gem_kinds(message.content)
        if not missing:
            return
        missing = await self.allowed_kinds(missing)
        if not missing:
            return
        picks = self.pick_gem_ids(self.available_gems, missing)
        if picks:
            await self.send_gem_use(picks)
            return
        checkable = [kind for kind in missing if self.kind_check_allowed(kind)]
        if checkable:
            await self.request_inventory(checkable)

    async def read_inventory_safely(self, message, text):
        try:
            await self.read_inventory(message, text)
        except Exception as e:
            await self.bot.log(
                f"Error - {self.bot.username}: inventory not read ({type(e).__name__}: {e})",
                "#c25560",
            )
            traceback.print_exc()

    async def read_inventory(self, message, text):
        message_id = getattr(message, "id", None)
        if message_id is not None:
            # One reply can reach this cog twice, as a message and as its edit
            if message_id in self.read_inventory_ids:
                return
            self.read_inventory_ids.append(message_id)

        requested, kinds = self.inventory_check, self.pending_kinds
        self.inventory_check = False
        self.pending_kinds = None
        if requested:
            # Task 4: make commands trigger stat instead of manual to avoid such issues and also avoids permanent stop incase of fails
            # The reply is here: release the sends held for it first, or the boxes and gems queued below wait for the timeout
            await self.bot.ch.resume_stat()
        await self.bot.ch.remove_queue(id="inv")
        self.available_gems = find_gems_available(text)
        self.available_box_items = find_box_items_available(text)
        box_commands = make_inventory_box_commands(
            self.available_box_items,
            self.bot.settings_dict.autoUse,
            self.bot.alias,
        )
        fabled_commands = [
            command for command in box_commands
            if command["id"] == "fabledLootbox"
        ]
        if fabled_commands:
            fabled_commands[0]["repeat_remaining"] = len(fabled_commands)
            await self.bot.ch.put_queue(fabled_commands[0])
        for command in box_commands:
            if command["id"] != "fabledLootbox":
                await self.bot.ch.put_queue(command)
        if requested:
            await self.use_pending_gems(kinds)
            await self.bot.sleep_till(
                self.bot.settings_dict.cooldowns.briefCooldown
            )


async def setup(bot):
    await bot.add_cog(Gems(bot))
