"""Shared test helpers: the status of a fixture change, and git reads."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import ENG_TOKEN, PM_TOKEN, people_map
from pipeline_viewer import config, places, status

PM = people_map()[PM_TOKEN]
ENG = people_map()[ENG_TOKEN]


def place(fx: dict, name: str) -> places.Place:
    return places.list_changes(fx["repo"], fx["wt"])[name]


def status_fn(fx: dict, name: str, epic_dir_for=None):
    p = place(fx, name)
    cfg = config.load(fx["repo"])

    def fresh():
        return status.change_status(p, cfg, people_map(), epic_dir_for)

    return p, fresh


def stop(fx: dict, name: str, n: int):
    return status_fn(fx, name)[1]().stops[n - 1]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def roles(fx: dict, n: int) -> tuple[str, ...]:
    return config.load(fx["repo"]).stop(n).roles
