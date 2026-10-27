from app.text import initials, slug, truncate, word_count


def test_text_helpers():
    assert slug("Déjà Vu -- 2") == "dj-vu-2"
    assert word_count("") == 0
    assert truncate("abcd", 4) == "abcd"
    assert truncate("abcde", 4) == "abc…"
    assert initials("grace brewster hopper") == "GBH"
