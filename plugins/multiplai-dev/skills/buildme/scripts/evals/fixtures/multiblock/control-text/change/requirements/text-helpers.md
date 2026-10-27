## ADDED Requirements

### Requirement: Slug
`app.text.slug(text)` SHALL lowercase the words of `text` and join them
with hyphens, dropping every character that is not a letter, digit or
space.

#### Scenario: Slug
- **WHEN** `slug("  Hello, World! ")` is called
- **THEN** it returns `"hello-world"`

### Requirement: Word count
`app.text.word_count(text)` SHALL return the number of
whitespace-separated words.

#### Scenario: Count
- **WHEN** `word_count("a  b\tc")` is called
- **THEN** it returns `3`

### Requirement: Truncate
`app.text.truncate(text, limit)` SHALL return `text` unchanged when it
has at most `limit` characters, and otherwise its first `limit - 1`
characters followed by `"…"`.

#### Scenario: Truncate
- **WHEN** `truncate("abcdef", 4)` is called
- **THEN** it returns `"abc…"`

### Requirement: Initials
`app.text.initials(name)` SHALL return the upper-cased first letter of
each word, joined with no separator.

#### Scenario: Initials
- **WHEN** `initials("ada king lovelace")` is called
- **THEN** it returns `"AKL"`
