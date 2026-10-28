"""Questions asked on the page and the session's answers, for everyone.

Pure: the box's inbox rows (`kind: ask`) and outbox rows in, plain dicts
out. The page shows them to every viewer (`GET /api/questions`), and
`record.answer` writes each answered one into the stop's review file.
"""

from __future__ import annotations


def questions(
    inbox: list[dict],
    outbox: list[dict],
    name: str | None = None,
    stop: int | None = None,
) -> list[dict]:
    """Every `ask` row (of change *name*, at *stop*, when given), oldest
    first, with its answer joined from the outbox rows that reply to it.
    `answer` is None until a row arrives; `done` is the last row's flag."""
    replies: dict[str, list[dict]] = {}
    for row in outbox:
        rid = row.get("reply_to")
        if isinstance(rid, str) and isinstance(row.get("text"), str):
            replies.setdefault(rid, []).append(row)
    out = []
    for row in inbox:
        if row.get("kind") != "ask" or not isinstance(row.get("id"), str):
            continue
        if name is not None and row.get("change") != name:
            continue
        if stop is not None and row.get("stop") != stop:
            continue
        author = row.get("author") or {}
        parts = replies.get(row["id"], [])
        answer = None
        for part in parts:
            text = part["text"]
            if answer is None:
                answer = text
            elif answer.endswith("\n"):
                answer += text
            else:
                answer += "\n\n" + text
        commit = next((p["commit"] for p in reversed(parts) if p.get("commit")), None)
        out.append(
            {
                "id": row["id"],
                "change": row.get("change"),
                "stop": row.get("stop"),
                "doc": row.get("doc"),
                "section": row.get("section"),
                "author": {"name": author.get("name"), "role": author.get("role")},
                "asked_at": row.get("ts"),
                "text": row.get("text") or "",
                "answer": answer,
                "answered_at": parts[-1].get("ts") if parts else None,
                "done": bool(parts and parts[-1].get("done")),
                "commit": commit,
            }
        )
    return out


def unrecorded(qs: list[dict], recorded_ids: set[str]) -> list[dict]:
    """Answered questions at a stop whose answer is not in git yet: a final
    answer, a stop to file it under, and no commit with its `Question:`."""
    return [
        q
        for q in qs
        if q["done"] and q["stop"] is not None and q["id"] not in recorded_ids
    ]
