def select_lootbox_action(item_code: str, settings) -> tuple[str, str] | None:
    """Select the command ID and arguments for a lootbox item.

    Args:
        item_code: The item code to open.
        settings: AutoUse settings with the lootbox and crate flags.

    Returns:
        The command ID and arguments, or None when no action is enabled.

    The "all" flags take priority over the legacy single-item flags.
    """
    if item_code == "050":
        if settings.lootboxAll:
            return ("lootbox", "all")
        elif settings.lootbox:
            return ("lootbox", "")
        else:
            return None
    elif item_code == "100":
        if settings.crateAll:
            return ("crate", "all")
        elif settings.crate:
            return ("crate", "")
        else:
            return None
    elif item_code == "049":
        if settings.fabledLootbox:
            return ("fabledLootbox", settings.fabledLootboxCommand)
        else:
            return None
    elif item_code == "099":
        if settings.bCrateAll:
            return ("bcrate", "all")
        else:
            return None
    else:
        return None
