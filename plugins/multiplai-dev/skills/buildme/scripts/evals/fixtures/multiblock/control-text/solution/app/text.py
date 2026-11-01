def slug(text):
    kept = "".join(c for c in text.lower() if c.isalpha() or c.isdigit() or c == " ")
    return "-".join(kept.split())


def word_count(text):
    return len(text.split())


def truncate(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def initials(name):
    return "".join(word[0].upper() for word in name.split())
