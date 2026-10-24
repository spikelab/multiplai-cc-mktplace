"""The usage-window check: a call starts only when the Claude usage window can pay for it.

The HTTP call (`window._fetch`) and the credentials file are stubbed; nothing
here reaches the endpoint. conftest stubs `window.read` for every other test,
and `real_read` puts the real one back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
from types import SimpleNamespace

import paths_stub
import pytest
from conftest import REAL_WINDOW_READ
from review_pipeline import budget, sdk, window
from test_reported_finders import DIMENSIONS, make_repo, resume, review, saved

TOKEN = "stub-oauth-token-7f3a9c"
RESETS = "2026-10-24T15:00:00+00:00"


def answer(utilization: float = 50.0, remaining: float | None = None) -> dict:
    """The endpoint's JSON as it was on Spike's account, with *remaining* in both windows."""
    window_ = {
        "utilization": utilization,
        "resets_at": RESETS,
        "limit_dollars": None,
        "used_dollars": None,
        "remaining_dollars": remaining,
    }
    return {
        "five_hour": window_,
        "seven_day": {**window_, "utilization": 10.0},
        "seven_day_opus": None,
        "seven_day_sonnet": None,
    }


class Endpoint:
    """Stands in for `window._fetch`: returns `answers` in order (the last one again), or raises `error`."""

    def __init__(self, *answers: dict, error: Exception | None = None):
        self.answers, self.error = list(answers), error
        self.tokens: list[str] = []

    def __call__(self, token: str) -> dict:
        self.tokens.append(token)
        if self.error is not None:
            raise self.error
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


@pytest.fixture
def real_read(monkeypatch, tmp_path):
    """The real `window.read` over a stub credentials file and a clock the test moves."""
    config = tmp_path / "claude-config"
    config.mkdir()
    (config / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": TOKEN}})
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setattr(window, "read", REAL_WINDOW_READ)
    monkeypatch.setattr(window, "last_error", "")
    now = [1000.0]
    monkeypatch.setattr(window, "_clock", lambda: now[0])
    monkeypatch.setattr(window, "_gates", {})
    return now


def use(monkeypatch, endpoint: Endpoint) -> Endpoint:
    monkeypatch.setattr(window, "_fetch", endpoint)
    return endpoint


# --- reading ----------------------------------------------------------------------------------------------


def test_utilization_is_read_on_a_0_to_100_scale_and_null_windows_are_left_out():
    (five, seven) = window.parse(answer(utilization=76.0, remaining=3.5))
    assert (five.name, five.utilization, five.resets_at, five.remaining_dollars) == (
        "five_hour",
        76.0,
        RESETS,
        3.5,
    )
    assert seven.name == "seven_day" and seven.utilization == 10.0
    assert window.parse({"five_hour": None, "seven_day": {"utilization": "high"}}) == []
    # 76% used leaves 24 points: at $2 a point, $48.
    g = window.Gate(rates={"five_hour": 2.0})
    reading = window.Reading("five_hour", 76.0, RESETS, None)
    assert g.left([reading]) == (reading, pytest.approx(48.0))


def test_the_token_goes_into_the_request_and_a_reading_is_kept_30_seconds(
    real_read, monkeypatch
):
    endpoint = use(monkeypatch, Endpoint(answer(4.0), answer(9.0)))
    assert window.read()[0].utilization == 4.0
    real_read[0] += window.READ_EVERY_S - 1
    assert window.read()[0].utilization == 4.0
    real_read[0] += 2
    assert window.read()[0].utilization == 9.0
    assert endpoint.tokens == [TOKEN, TOKEN]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (urllib.error.HTTPError(window.USAGE_URL, 401, "no", {}, None), "HTTP 401"),
        (TimeoutError("timed out"), "TimeoutError"),
        (ValueError("not JSON"), "ValueError"),
    ],
)
def test_a_failed_read_is_none_with_why(real_read, monkeypatch, error, reason):
    use(monkeypatch, Endpoint(error=error))
    assert window.read() is None and window.last_error == reason


def test_no_credentials_file_is_a_failed_read_naming_the_file(real_read, monkeypatch):
    window.credentials_path().unlink()
    endpoint = use(monkeypatch, Endpoint(answer()))
    assert window.read() is None and endpoint.tokens == []
    assert (
        window.last_error
        == f"no claudeAiOauth.accessToken in {window.credentials_path()}"
    )


# --- what a window has left --------------------------------------------------------------------------------


def test_remaining_dollars_is_what_is_left_until_a_rate_is_measured():
    g = window.Gate()
    first = window.Reading("five_hour", 10.0, RESETS, 30.0)
    g.learn([first])
    assert g.left([first]) == (first, 30.0)


def test_with_no_remaining_dollars_usd_per_point_comes_from_two_readings_2_points_apart():
    g = window.Gate()
    g.learn([window.Reading("five_hour", 10.0, RESETS, None)])
    assert g.left([window.Reading("five_hour", 10.0, RESETS, None)]) is None
    g.spent = 3.0
    g.learn(
        [window.Reading("five_hour", 11.0, RESETS, None)]
    )  # 1 point: too close to measure
    assert "five_hour" not in g.rates
    g.spent = 5.0
    later = window.Reading("five_hour", 12.0, RESETS, None)
    g.learn([later])
    assert g.rates["five_hour"] == pytest.approx(2.5)  # $5 over 2 points
    assert g.left([later]) == (later, pytest.approx(88 * 2.5))


def test_the_measured_rate_wins_over_remaining_dollars_and_both_are_logged(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(window, "progress_line", lines.append)
    g = window.Gate(spent=0.0)
    g.learn([window.Reading("five_hour", 10.0, RESETS, 500.0)])
    g.spent = 4.0
    now = window.Reading("five_hour", 12.0, RESETS, 500.0)
    g.learn([now])
    assert g.left([now]) == (now, pytest.approx(88 * 2.0))
    assert lines == [
        (
            f"WINDOW five_hour: utilization 12%, measured $176.00 left (usd_per_point $2.00), "
            f"remaining_dollars $500.00, resets {RESETS}"
        )
    ]


def test_a_new_reset_time_starts_the_measurement_again():
    g = window.Gate()
    g.learn([window.Reading("five_hour", 10.0, RESETS, None)])
    g.spent = 5.0
    g.learn([window.Reading("five_hour", 12.0, RESETS, None)])
    assert "five_hour" in g.rates
    g.learn([window.Reading("five_hour", 1.0, "2026-10-24T20:00:00+00:00", None)])
    assert "five_hour" not in g.rates


def test_a_reset_time_that_moves_by_under_a_second_between_reads_is_the_same_window():
    # Three reads of one window on 2026-10-24: the endpoint's reset time moved each time.
    g = window.Gate()
    g.learn(
        [window.Reading("five_hour", 9.0, "2026-10-24T13:40:00.364858+00:00", None)]
    )
    g.spent = 26.0
    g.learn(
        [window.Reading("five_hour", 22.0, "2026-10-24T13:39:59.597598+00:00", None)]
    )
    assert g.rates["five_hour"] == pytest.approx(2.0)  # $26 over 13 points
    g.spent = 30.0
    g.learn(
        [window.Reading("five_hour", 24.0, "2026-10-24T13:40:00.181188+00:00", None)]
    )
    assert g.rates["five_hour"] == pytest.approx(2.0)  # $30 over 15 points


def test_a_reset_time_that_cannot_be_read_is_compared_as_text():
    g = window.Gate()
    g.learn([window.Reading("five_hour", 10.0, "soon", None)])
    g.spent = 5.0
    g.learn([window.Reading("five_hour", 12.0, "soon", None)])
    assert "five_hour" in g.rates
    g.learn([window.Reading("five_hour", 14.0, "later", None)])
    assert "five_hour" not in g.rates


# --- admitting calls ----------------------------------------------------------------------------------------


async def test_a_call_that_does_not_fit_waits_while_another_runs_and_starts_when_it_ends(
    real_read, monkeypatch
):
    # $2 left: 90% of it fits one $1.19 call, not two.
    use(monkeypatch, Endpoint(answer(remaining=2.0)))
    first = await window.admit("find:callers")
    second = asyncio.create_task(window.admit("find:history"))
    await asyncio.sleep(0.05)
    assert not second.done() and window.gate().calls == 1
    window.release(first)
    assert await asyncio.wait_for(second, 1) == pytest.approx(
        window.DEFAULT_ESTIMATES["find:piece"]
    )
    assert window.gate().calls == 1


async def test_with_nothing_running_a_call_that_does_not_fit_raises_window_full(
    real_read, monkeypatch
):
    use(monkeypatch, Endpoint(answer(utilization=99.0, remaining=0.5)))
    with pytest.raises(sdk.WindowFullError) as raised:
        await window.admit("find:callers")
    e = raised.value
    assert isinstance(e, sdk.UsageLimitError)
    assert (e.limit, e.resets) == ("five_hour", RESETS)
    assert (
        f"utilization 99%, about $0.50 left, next call about $1.19); it resets {RESETS}"
        in str(e)
    )
    assert window.gate().calls == 0


async def test_an_unreadable_endpoint_admits_every_call_and_notices_once(
    real_read, monkeypatch, capsys
):
    use(
        monkeypatch,
        Endpoint(error=urllib.error.HTTPError(window.USAGE_URL, 500, "x", {}, None)),
    )
    costs = await asyncio.gather(*(window.admit("find:tests") for _ in range(12)))
    real_read[0] += 60
    costs.append(await window.admit("verify"))
    assert window.gate().calls == 13
    err = capsys.readouterr().err
    assert err.count("NOTICE: usage window not readable") == 1
    assert (
        "NOTICE: usage window not readable (HTTP 500); calls are limited only by concurrency"
        in err
    )
    for cost in costs:
        window.release(cost)
    assert window.gate().calls == 0


async def test_this_runs_spend_is_estimated_from_the_ledger_once_a_call_returned():
    ledger = budget.get_budget()
    assert window.estimate("find:callers") == window.DEFAULT_ESTIMATES["find:piece"]
    assert (
        window.estimate("find:callers-convert")
        == window.DEFAULT_ESTIMATES["find:piece-convert"]
    )
    ledger.record(SimpleNamespace(cost_usd=3.0), label="find:callers")
    ledger.record(SimpleNamespace(cost_usd=1.0), label="find:callers")
    assert window.estimate("find:callers") == pytest.approx(2.0)


# --- through the command line --------------------------------------------------------------------------------


class RunAgent:
    """Stands in for `sdk.run_agent`, below the real `agent_call_text` and `agent_call_structured`, so every
    call passes through `sdk._run` and the window check. Every report says NO FINDINGS, so no convert,
    verify or merge call follows."""

    def __init__(self):
        self.labels: list[str] = []

    async def __call__(self, prompt, **kwargs):
        self.labels.append(kwargs.get("label", ""))
        return SimpleNamespace(
            text="NO FINDINGS",
            tool_calls=(),
            turns=1,
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, cost_usd=0.5),
        )


@pytest.fixture
def run_agent(monkeypatch) -> RunAgent:
    fake = RunAgent()
    monkeypatch.setattr(sdk, "agent_call_text", paths_stub.REAL_AGENT_CALL_TEXT)
    monkeypatch.setattr(sdk, "run_agent", fake)
    return fake


def finder_labels(fake: RunAgent) -> list[str]:
    return [label for label in fake.labels if label.startswith("find:")]


def test_a_full_window_saves_the_state_prints_resume_and_the_reset_and_resume_asks_the_rest(
    tmp_path, real_read, monkeypatch, run_agent, capsys, caplog
):
    """Row S7: a stop before a call starts keeps every answer before it; the resume asks only the rest."""
    caplog.set_level(logging.DEBUG)
    repo, base, head = make_repo(tmp_path, 2)
    use(monkeypatch, Endpoint(answer(utilization=99.0, remaining=0.5)))
    assert review(repo, base, head, tmp_path / "out") == 6
    err = capsys.readouterr().err
    target_dir, state = saved(tmp_path / "out")
    assert f"it resets {RESETS}" in err and "five_hour usage window" in err
    assert f"python -m review_pipeline resume {target_dir} --trust-repo" in err
    assert not state.past("find") and finder_labels(run_agent) == []

    real_read[0] += 60
    assert resume(target_dir) == 6  # still full: stops again before starting a call
    assert finder_labels(run_agent) == []

    use(monkeypatch, Endpoint(answer(utilization=20.0, remaining=500.0)))
    real_read[0] += 60
    assert resume(target_dir) == 0
    # 2 pieces x 6 dimensions, and the one path the stub mapping gives, each asked once.
    assert len(finder_labels(run_agent)) == 2 * 6 + 1
    _, finished = saved(tmp_path / "out")
    assert finished.past("find")
    assert sorted(finished.finder_reports) == sorted(
        [*(f"{d}@{i}" for d in DIMENSIONS for i in (1, 2)), "path@p1"]
    )
    logged = caplog.text + err + capsys.readouterr().err
    logged += (target_dir / "progress.log").read_text()
    assert TOKEN not in logged


def test_an_unreadable_endpoint_runs_the_review_with_one_notice(
    tmp_path, real_read, monkeypatch, run_agent, capsys, caplog
):
    """Row S8."""
    caplog.set_level(logging.DEBUG)
    repo, base, head = make_repo(tmp_path, 2)
    use(
        monkeypatch,
        Endpoint(error=urllib.error.HTTPError(window.USAGE_URL, 401, "no", {}, None)),
    )
    assert review(repo, base, head, tmp_path / "out") == 0
    out = capsys.readouterr()
    target_dir, _ = saved(tmp_path / "out")
    progress = (target_dir / "progress.log").read_text()
    notice = "NOTICE: usage window not readable (HTTP 401)"
    assert out.err.count(notice) == 1 and progress.count(notice) == 1
    assert len(finder_labels(run_agent)) == 2 * 6 + 1
    assert TOKEN not in caplog.text + out.err + out.out + progress
