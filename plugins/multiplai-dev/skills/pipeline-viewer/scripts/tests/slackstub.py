"""A stand-in for slack_sdk.WebClient that records every post."""

from __future__ import annotations

import itertools

import slack_sdk

POSTS: list[dict] = []
TOKENS: list[str] = []
_ts = itertools.count(1)


class FakeClient:
    def __init__(self, token=None, **_):
        TOKENS.append(token)

    def chat_postMessage(self, **kwargs):
        POSTS.append(kwargs)
        return {"ok": True, "ts": f"1700000000.{next(_ts):06d}"}


class FailingClient(FakeClient):
    def chat_postMessage(self, **kwargs):
        raise slack_sdk.errors.SlackApiError(
            "boom", {"ok": False, "error": "channel_not_found"}
        )


def install(monkeypatch, cls=FakeClient):
    POSTS.clear()
    TOKENS.clear()
    monkeypatch.setattr(slack_sdk, "WebClient", cls)
    return POSTS
