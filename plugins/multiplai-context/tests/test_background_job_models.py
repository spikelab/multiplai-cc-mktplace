"""Background jobs keep their own model when a session model is around.

multiplai-core 0.21 made ``run_agent`` fall back, when it is handed no model,
to ``default_model()`` (``MULTIPLAI_MODEL``, then ``multiplai.conf``, then the
Claude Code session's model) and, when it is handed no effort, to
``default_effort()``. Session-started skills want that. Background jobs do
not: nobody chose a model for them in a session, so each one must reach the
SDK with the model it picked itself, whatever the session or
``MULTIPLAI_MODEL`` says.

Each job below is driven through its own code, with only the SDK call itself
replaced: ``multiplai_core.agent_runner._safe_query``, which is where
``run_agent`` hands the finished ``ClaudeAgentOptions`` to
``claude_agent_sdk.query``. Everything above it runs for real, including
``create_client``, ``AgentSDKClient.query``'s defaults and ``run_agent``'s
fallback, so the model recorded is the one the CLI would have been started
with. Every job is run three times: with nothing set, with only the session's
values pinned, and with ``MULTIPLAI_MODEL`` / ``MULTIPLAI_EFFORT`` set as well.

Each job's effort is its own plugin option, ``<job>_effort`` (``lib/job_effort.py``).
Unset, the job sends no effort and each call receives whatever
``default_effort()`` finds (``test_effort_follows_the_session_when_a_job_sets_none``);
set, every call the job makes receives it, whatever the session or
``MULTIPLAI_EFFORT`` says (``test_job_effort_option_wins``).
"""

import asyncio
import io
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import SCRIPTS_DIR, import_script

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.job_effort import EFFORT_OPTIONS
from multiplai_core.env import CURRENT_MODEL
from multiplai_core.model_client import DEFAULT_MODEL as CLIENT_DEFAULT_MODEL
from multiplai_core.plugin_options import option_var

SESSION_MODEL = "claude-test-session"
SESSION_EFFORT = "low"
CONFIGURED_MODEL = "claude-test-default"
CONFIGURED_EFFORT = "max"

#: The three environments each job runs in. ``unset`` is the baseline the
#: other two must not move the model away from.
ENV_STATES: dict[str, dict[str, str]] = {
    "unset": {},
    "session": {
        "MULTIPLAI_SESSION_MODEL": SESSION_MODEL,
        "MULTIPLAI_SESSION_EFFORT": SESSION_EFFORT,
    },
    "session+configured": {
        "MULTIPLAI_SESSION_MODEL": SESSION_MODEL,
        "MULTIPLAI_SESSION_EFFORT": SESSION_EFFORT,
        "MULTIPLAI_MODEL": CONFIGURED_MODEL,
        "MULTIPLAI_EFFORT": CONFIGURED_EFFORT,
    },
}

#: What ``default_effort()`` returns in each state, i.e. what a call that
#: passes no effort ends up with.
DEFAULT_EFFORT_BY_STATE = {
    "unset": None,
    "session": SESSION_EFFORT,
    "session+configured": CONFIGURED_EFFORT,
}


# --- the SDK boundary --------------------------------------------------------


@dataclass(frozen=True)
class SdkCall:
    model: str | None
    effort: str | None


class SdkRecorder:
    """Stands in for ``_safe_query``: records the options, returns *reply*."""

    def __init__(self) -> None:
        self.calls: list[SdkCall] = []
        self.reply: Callable[[str], str] = lambda _system: ""

    async def fake_safe_query(self, sdk, *, prompt, options):
        self.calls.append(
            SdkCall(
                model=options.model,
                effort=getattr(options, "effort", None),
            )
        )
        text = self.reply(options.system_prompt or "")
        if text:
            yield sdk.AssistantMessage(
                content=[sdk.TextBlock(text=text)], model=options.model or ""
            )


@pytest.fixture
def sdk(monkeypatch):
    from multiplai_core import agent_runner

    recorder = SdkRecorder()
    monkeypatch.setattr(agent_runner, "_safe_query", recorder.fake_safe_query)
    # No per-call CLI debug log files: there is no CLI.
    monkeypatch.setenv("MULTIPLAI_SDK_DEBUG_LOG", "off")
    return recorder


def _use_workspace(root: Path, monkeypatch):
    """Point every plugin path at a fresh workspace under *root*."""
    from multiplai_core.paths import _reset_cache, get_paths

    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_WORKSPACE_DIR", str(root / "ws"))
    _reset_cache()
    paths = get_paths()
    for d in (
        paths.memory_dir(),
        paths.diary_dir(),
        paths.now_dir(),
        paths.learnings_dir,
        paths.dreams_dir(),
        paths.catalogs_dir(),
    ):
        d.mkdir(parents=True, exist_ok=True)
    return paths


def _transcript(path: Path, cwd: str = "/work/proj") -> Path:
    """A two-turn Claude Code transcript, timestamped just now."""
    now = datetime.now(UTC)

    def turn(role, text, ts):
        rec = {
            "type": role,
            "timestamp": ts.isoformat(),
            "cwd": cwd,
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
        }
        if role == "assistant":
            rec["message"]["usage"] = {"input_tokens": 1000, "output_tokens": 10}
        return rec

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            json.dumps(r)
            for r in (
                turn("user", "please fix the auth bug", now - timedelta(minutes=5)),
                turn("assistant", "fixed the auth bug in login.py", now),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _diary(paths, cwd: str = "/work/proj") -> None:
    ts = datetime.now(UTC).isoformat()
    (paths.diary_dir() / "today.md").write_text(
        f"# Diary — test\n\n## Session: s1 — {ts} — {cwd}\n\n[{ts}]\n\n"
        "Fixed the auth bug.\n",
        encoding="utf-8",
    )


UNIT_REPLY = (
    "<unit><timestamp>{ts}</timestamp>"
    "<diary>\nFixed the auth bug.\n</diary></unit>"
    "<disposition>state: done\nreason: fixed</disposition>"
    "<utilisation></utilisation>"
)


# --- one driver per job ------------------------------------------------------
# Each runs the job from the function that owns its model choice down, in a
# fresh directory, and returns nothing: the recorder holds what reached the SDK.


def _maintainer_status(root, monkeypatch, sdk):
    """memory_maintainer.run_status → pick_model("haiku", "maintainer_status")."""
    import memory_maintainer as mm

    paths = _use_workspace(root, monkeypatch)
    _diary(paths)
    result = mm.run_status(paths.diary_dir())
    assert result.ran, result.detail


def _maintainer_judge(root, monkeypatch, sdk):
    """memory_maintainer.run_utilisation_judge → pick_model("haiku", "utilisation_judge")."""
    import memory_maintainer as mm
    from lib import utilisation as util

    _use_workspace(root, monkeypatch)
    data = root / "judge-data"
    data.mkdir()
    util.upsert_session(
        util.utilisation_path(data),
        "s1",
        {
            "injected": [
                {
                    "file": "dev.md",
                    "section": "Testing",
                    "bytes": 100,
                    "turn": 1,
                    "co_picks": 1,
                }
            ],
            "transcript": str(_transcript(root / "t.jsonl")),
        },
    )
    result = mm.run_utilisation_judge(data)
    assert result.ran, result.detail


def _maintainer_doctor(root, monkeypatch, sdk):
    """memory_maintainer.run_doctor → pick_model("haiku", "memory_doctor")."""
    import memory_maintainer as mm

    _use_workspace(root, monkeypatch)
    memory = root / "doctor-memory"
    data = root / "doctor-data"
    memory.mkdir()
    data.mkdir()
    deploy = (
        "- Cloud Run rolling deploys health-check the new revision "
        "first, then shift traffic on success, and roll back "
        "automatically.\n"
    )
    (memory / "alpha.md").write_text("# Alpha\n\n" + deploy, encoding="utf-8")
    (memory / "beta.md").write_text(
        "# Beta\n\n" + deploy.replace(", and", " and"), encoding="utf-8"
    )
    (memory / "notes.md").write_text(
        "# Notes\n\n- " + "a note about postgres. " * 40 + "\n", encoding="utf-8"
    )
    result = mm.run_doctor(memory, data, root / "doctor-dreams")
    assert result.ran, result.detail


def _router(root, monkeypatch, sdk):
    """lib.memory_router LLMRouter (memory_router=llm) → DEFAULT_ROUTER_MODEL."""
    from lib.memory_router import LLMRouter, create_router

    _use_workspace(root, monkeypatch)
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MEMORY_ROUTER", "llm")
    router = create_router()
    assert isinstance(router, LLMRouter), type(router)
    router.select_multi(
        "how do I deploy to Cloud Run?",
        None,
        {
            "memory": [{"source": "dev.md", "description": "deploy notes"}],
            "skills": [],
            "resources": [],
        },
    )


def _catalogs(root, monkeypatch, sdk):
    """generators: every LLM-backed generator's _call_llm → catalog_model."""
    from generators import dispatcher
    from generators.config import load_catalog_config

    _use_workspace(root, monkeypatch)

    async def run():
        client = await dispatcher._create_model_client()
        assert not getattr(client, "is_stub", False), "no real client was built"
        config = load_catalog_config()
        for name in ("memory", "diary", "skills"):
            gen = dispatcher.GENERATOR_CLASSES[name](config=config, model_client=client)
            await gen._call_llm(f"Catalog this {name} source.")

    asyncio.run(run())


def _checkpoint(root, monkeypatch, sdk):
    """checkpoint_writer.write_checkpoint → cfg.model or model_client.DEFAULT_MODEL."""
    _use_workspace(root, monkeypatch)
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_DATA_DIR", str(root / "cp-data"))
    from multiplai_core.paths import _reset_cache

    _reset_cache()
    checkpoint_writer = import_script("checkpoint_writer", "checkpoint_writer.py")
    asyncio.run(
        checkpoint_writer.write_checkpoint(
            {
                "session_id": "s1",
                "transcript_path": str(_transcript(root / "t.jsonl")),
                "cwd": "/work/proj",
                "tokens": 110_000,
                "reason": "band",
            }
        )
    )


DREAM_DRAFT = """\
# Processed Learnings — 2026-07-30

## Updates for `dev.md`

### 1. A drafted lesson
**Change:** add
> Do the thing.

**Source:** 2026-07-30.md:3
"""


def _dream(root, monkeypatch, sdk):
    """dream.dream_report → client.query with no model (the client's default)."""
    import importlib.util

    paths = _use_workspace(root, monkeypatch)
    (paths.memory_dir() / "dev.md").write_text("# Dev\n\n## Tooling\n")
    (paths.learnings_dir / "2026-07-30.md").write_text(
        "\n".join(
            f"## Session Learnings — 2026-07-30T0{i}:00:00+00:00\n"
            f"Session: s{i}\n"
            f"- **[trust: verified]** PATTERN Lesson {i}. → Target: dev.md\n\n---\n"
            for i in range(3)
        )
    )
    sdk.reply = lambda system: (
        "NOOP" if system.startswith("You are a strict editor") else DREAM_DRAFT
    )
    # A fresh module per run: the run lock is module state.
    spec = importlib.util.spec_from_file_location(
        f"dream_models_{root.name}", SCRIPTS_DIR / "dream.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        asyncio.run(mod.dream_report())
    finally:
        if mod._RUN_LOCK_FD is not None:
            os.close(mod._RUN_LOCK_FD)
            mod._RUN_LOCK_FD = None


def _extract_learnings(root, monkeypatch, sdk):
    """extract_learnings.extract → extraction, then the now/ refresh (synthesize)."""
    import extract_learnings as el

    _use_workspace(root, monkeypatch)
    sdk.reply = lambda _system: UNIT_REPLY.format(
        ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    monkeypatch.setattr(
        el.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "s1",
                    "cwd": "/work/proj",
                    "transcript_path": str(_transcript(root / "t.jsonl")),
                }
            )
        ),
    )
    assert asyncio.run(el.extract()) is True


def _backfill(root, monkeypatch, sdk):
    """backfill.backfill → extraction, the now/ refresh and the diary catalog."""
    from backfill import backfill

    _use_workspace(root, monkeypatch)
    config_dir = root / "claude-config"
    _transcript(config_dir / "projects" / "proj" / "session-b1.jsonl")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    sdk.reply = lambda _system: UNIT_REPLY.format(
        ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    summary = asyncio.run(
        backfill(
            datetime.now(UTC) - timedelta(days=1),
            run_catalogs=True,
            run_now=True,
        )
    )
    assert summary["scanned"] == 1, summary


@dataclass(frozen=True)
class Job:
    name: str
    drive: Callable
    #: Every model the job's calls reach the SDK with, nothing set.
    models: frozenset[str]
    #: The ``<job>_effort`` options (lib/job_effort.py) that govern its calls.
    effort_options: tuple[str, ...]


HAIKU = CURRENT_MODEL["haiku"]

JOBS = [
    Job(
        "memory_maintainer status",
        _maintainer_status,
        frozenset({HAIKU}),
        ("now_effort",),
    ),
    Job(
        "memory_maintainer utilisation judge",
        _maintainer_judge,
        frozenset({HAIKU}),
        ("utilisation_judge_effort",),
    ),
    Job(
        "memory_maintainer doctor",
        _maintainer_doctor,
        frozenset({HAIKU}),
        ("memory_doctor_effort",),
    ),
    Job("memory router", _router, frozenset({"claude-haiku-5-5"}), ("router_effort",)),
    Job("catalogs", _catalogs, frozenset({"claude-sonnet-5-5"}), ("catalog_effort",)),
    Job(
        "checkpoint",
        _checkpoint,
        frozenset({CLIENT_DEFAULT_MODEL}),
        ("checkpoint_effort",),
    ),
    Job("dream", _dream, frozenset({CLIENT_DEFAULT_MODEL}), ("dream_effort",)),
    Job(
        "extract_learnings",
        _extract_learnings,
        frozenset({CLIENT_DEFAULT_MODEL}),
        ("extraction_effort", "now_effort"),
    ),
    Job(
        "backfill",
        _backfill,
        frozenset({CLIENT_DEFAULT_MODEL, "claude-sonnet-5-5"}),
        ("extraction_effort", "now_effort", "catalog_effort"),
    ),
]

#: The effort an option sets in the tests: neither SESSION_EFFORT nor
#: CONFIGURED_EFFORT, so a call that receives it got it from the option.
OPTION_EFFORT = "medium"


def _run_in(state: str, job: Job, tmp_path: Path, monkeypatch, sdk) -> list[SdkCall]:
    for key in {k for env in ENV_STATES.values() for k in env}:
        monkeypatch.delenv(key, raising=False)
    for key, value in ENV_STATES[state].items():
        monkeypatch.setenv(key, value)
    root = tmp_path / state.replace("+", "-")
    root.mkdir()
    sdk.calls.clear()
    sdk.reply = lambda _system: ""
    job.drive(root, monkeypatch, sdk)
    assert sdk.calls, f"{job.name} made no model call with {state} env"
    return list(sdk.calls)


@pytest.fixture(params=JOBS, ids=lambda job: job.name.replace(" ", "-"))
def job(request):
    return request.param


def test_job_model_ignores_the_session_and_multiplai_model(
    job, tmp_path, monkeypatch, sdk
):
    baseline = _run_in("unset", job, tmp_path, monkeypatch, sdk)
    assert {c.model for c in baseline} == job.models

    for state in ("session", "session+configured"):
        calls = _run_in(state, job, tmp_path, monkeypatch, sdk)
        assert {c.model for c in calls} == job.models, (
            f"{job.name}: with {state} env the SDK got {sorted({c.model for c in calls})}"
        )


def test_effort_follows_the_session_when_a_job_sets_none(
    job, tmp_path, monkeypatch, sdk
):
    """With no ``<job>_effort`` option, each call gets ``default_effort()`` —
    none, then the session's, then ``MULTIPLAI_EFFORT``."""
    for name in EFFORT_OPTIONS:
        monkeypatch.delenv(option_var(name), raising=False)
    for state, expected in DEFAULT_EFFORT_BY_STATE.items():
        calls = _run_in(state, job, tmp_path, monkeypatch, sdk)
        assert {c.effort for c in calls} == {expected}, (job.name, state)


def test_job_effort_option_wins(job, tmp_path, monkeypatch, sdk):
    """``<job>_effort`` reaches every call the job makes, in every env state."""
    for name in job.effort_options:
        monkeypatch.setenv(option_var(name), OPTION_EFFORT)
    for state in ENV_STATES:
        calls = _run_in(state, job, tmp_path, monkeypatch, sdk)
        assert {c.effort for c in calls} == {OPTION_EFFORT}, (job.name, state)
        assert {c.model for c in calls} == job.models


def test_an_invalid_job_effort_is_ignored(tmp_path, monkeypatch, sdk):
    """A value that is no effort name falls back like an unset option."""
    router = next(j for j in JOBS if j.name == "memory router")
    monkeypatch.setenv(option_var("router_effort"), "turbo")
    for state, expected in DEFAULT_EFFORT_BY_STATE.items():
        calls = _run_in(state, router, tmp_path, monkeypatch, sdk)
        assert {c.effort for c in calls} == {expected}, state


def test_every_job_effort_option_is_declared():
    """Each option lib/job_effort.py reads is in plugin.json's userConfig."""
    manifest = json.loads(
        (SCRIPTS_DIR.parent / ".claude-plugin" / "plugin.json").read_text()
    )
    declared = manifest["userConfig"]
    for name in EFFORT_OPTIONS:
        entry = declared[name]
        assert entry["type"] == "string" and entry["default"] == ""
    assert {n for j in JOBS for n in j.effort_options} <= set(EFFORT_OPTIONS)
