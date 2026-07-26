# Prospective Memory

<!--
Intentions to come back to. Every other memory file answers "what is true?";
this one answers "what did I say I'd revisit?".

One intention per line, in one of two shapes:

  - [due: YYYY-MM-DD] What to do (captured YYYY-MM-DD)
  - [on: a condition in plain words] What to do (captured YYYY-MM-DD)

`due:` entries are surfaced automatically at SessionStart from a week before
the date, and stay surfaced while overdue.

`on:` entries are NOT evaluated — no code decides whether "when the runtime
updates" has happened, because a wrong guess fires the reminder at the wrong
time. They surface through normal memory routing when a prompt touches their
topic, and are re-listed every 30 days so they don't sink out of view. That
30-day clock runs from the last time the entry was actually shown (tracked
outside this file), not from a fixed calendar grid — so a day with no session
delays the re-listing, it does not skip it.

A trailing `<!-- comment -->` on an intention line is an annotation for you,
not part of the intention: it is stripped before the text is surfaced.

Entries are added through the normal pipeline (extraction → dream →
/dream-remember), not by hand-editing during a session. **Remove an intention
once it has been acted on or has stopped mattering** — nothing expires it
automatically, and a file of dead intentions is a nudge everyone learns to
ignore.
-->

## Dated

<!-- - [due: 2026-09-01] Re-check X now that Y has shipped (captured 2026-07-26) -->

## Conditional

<!-- - [on: the container image moves past v0.5] Re-run the config audit (captured 2026-07-26) -->
