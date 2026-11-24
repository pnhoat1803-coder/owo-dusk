"""Tells whether an OwO message is about a given account, whatever name OwO shows for it.

OwO writes names in bold, and Discord escapes markdown characters inside them, so the
account ``example_name_`` can show up in the message content as ``example_name\\_``. Names can
also differ in case, so the comparison ignores case.
"""

import re

# A backslash before ASCII punctuation is a markdown escape: \_ \* \~ \| \` and so on
_ESCAPED = re.compile(r"\\([!-/:-@\[-`{-~])")
# Bold, strikethrough and spoiler markers wrap a name, they are not part of it
_WRAPPERS = re.compile(r"\*\*|~~|\|\|")
# A name must not be glued to a longer name, such as amelia._lux_03 for amelia
_NAME_START = r"(?<!\w)(?<!\w\.)"
_NAME_END = r"(?!\w)(?!\.\w)"


def normalize(text):
    """The text as a reader sees it: escapes and wrappers removed, case folded"""
    text = _ESCAPED.sub(r"\1", text or "")
    return _WRAPPERS.sub("", text).casefold()


def template_in_text(text, template, name):
    """True if `template` appears in `text`, with `name` in place of {name}"""
    needle = normalize(name)
    if not needle:
        return False
    before, _, after = template.partition("{name}")
    pattern = (
        re.escape(normalize(before))
        + _NAME_START
        + re.escape(needle)
        + _NAME_END
        + re.escape(normalize(after))
    )
    return re.search(pattern, normalize(text)) is not None


class NameMatchMixin:
    """Mixed into the bot client, so cogs can ask whether a message is about this account"""

    def our_names(self, message=None):
        """Every name this account can show under: server nick, display name and username"""
        names = {self.user.name, self.user.display_name}
        member = getattr(getattr(message, "guild", None), "me", None)
        if member is not None:
            names |= {member.nick, member.display_name, member.name}
        return {name for name in names if name}

    def mentions_us(self, message):
        """OwO mentioned this account by its id"""
        mentions = (f"<@{self.user.id}>", f"<@!{self.user.id}>")
        return any(mention in message.content for mention in mentions)

    def is_addressed_to_us(self, message):
        """OwO mentioned this account, or replied to one of its messages"""
        if self.mentions_us(message):
            return True
        resolved = getattr(getattr(message, "reference", None), "resolved", None)
        author = getattr(resolved, "author", None)
        return author is not None and author.id == self.user.id

    def names_us(self, message):
        """The message mentions this account or shows one of its names. A reply alone does not count"""
        return self.mentions_us(message) or self.name_in(message.content, message)

    def is_for_us(self, message):
        """Whether an OwO message is about this account: by id when addressed to it, else by name"""
        if self.is_addressed_to_us(message):
            return True
        if getattr(message, "guild", None) is None:
            # Outside a server there is no nick to look for, so the message passes, as before
            return True
        return self.name_in(message.content, message)

    def name_in(self, text, message=None):
        """True if one of this account's names appears in `text`"""
        return any(
            template_in_text(text, "{name}", name) for name in self.our_names(message)
        )

    def name_phrase_in(self, text, template, message=None):
        """True if `template` appears in `text`, with one of this account's names in place of {name}"""
        return any(
            template_in_text(text, template, name) for name in self.our_names(message)
        )
