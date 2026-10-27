import re


def slug(text):
    return "-".join(re.sub(r"[^a-z0-9 ]", "", text.lower()).split())


def word_count(text):
    return len(text.split())


def truncate(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def initials(name):
    return "".join(word[0].upper() for word in name.split())
