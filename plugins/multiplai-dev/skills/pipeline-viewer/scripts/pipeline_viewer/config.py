"""`pipeline_viewer:` in the repo's `specs/config.yaml`.

```yaml
pipeline_viewer:
  reviewers:
    pm: [Ada Example]
    engineer: [Bo Example, Cy Example]
  stops:
    1: {reviewers: [pm]}
    3: {reviewers: [pm, engineer], decide_timeout_minutes: 600}
  ack_timeout_minutes: 120
  decide_timeout_minutes: 480
  slack: {channel: C0123456}
```

Names resolve through `people.json` (`reviewers_of`). A stop missing from
`stops` keeps its default reviewers; each timeout can be set per stop. A
missing file or section gives the defaults; a malformed one raises
`ConfigError` naming the key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from viewer_kit.people import Person

from .models import Reviewer

CONFIG_FILE = Path("specs") / "config.yaml"
SECTION = "pipeline_viewer"
ROLES = ("pm", "engineer")
DEFAULT_ACK_MINUTES = 120
DEFAULT_DECIDE_MINUTES = 480
DEFAULT_STOP_ROLES: dict[int, tuple[str, ...]] = {
    1: ("pm",),
    2: ("pm",),
    3: ("pm", "engineer"),
    4: ("engineer",),
    5: ("engineer",),
}


class ConfigError(ValueError):
    pass


@dataclass
class StopConfig:
    roles: tuple[str, ...]
    ack_timeout_minutes: int
    decide_timeout_minutes: int


@dataclass
class ViewerConfig:
    reviewers: dict[str, list[str]] = field(
        default_factory=lambda: {r: [] for r in ROLES}
    )
    stops: dict[int, StopConfig] = field(default_factory=dict)
    ack_timeout_minutes: int = DEFAULT_ACK_MINUTES
    decide_timeout_minutes: int = DEFAULT_DECIDE_MINUTES
    slack_channel: str | None = None

    def stop(self, n: int) -> StopConfig:
        return self.stops.get(n) or StopConfig(
            DEFAULT_STOP_ROLES[n], self.ack_timeout_minutes, self.decide_timeout_minutes
        )

    def reviewers_of(self, n: int, people: Mapping[str, Person]) -> list[Reviewer]:
        """The stop's reviewers: each name config lists for its roles, with
        the role from config, in config order. `invited` is false for a name
        that people.json does not hold."""
        known = {p.name for p in people.values()}
        out: list[Reviewer] = []
        for role in self.stop(n).roles:
            for name in self.reviewers.get(role, []):
                if not any(r.name == name and r.role == role for r in out):
                    out.append(Reviewer(name=name, role=role, invited=name in known))
        return out


def _minutes(value, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{SECTION}.{where} must be a whole number of minutes >= 1")
    return value


def _roles(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{SECTION}.{where} must be a list of roles (pm, engineer)")
    for role in value:
        if role not in ROLES:
            raise ConfigError(
                f"{SECTION}.{where}: unknown role {role!r}; want pm or engineer"
            )
    return tuple(value)


def parse(section: Mapping | None) -> ViewerConfig:
    cfg = ViewerConfig()
    if section is None:
        return cfg
    if not isinstance(section, Mapping):
        raise ConfigError(f"{SECTION} must be a mapping")
    unknown = set(section) - {
        "reviewers",
        "stops",
        "ack_timeout_minutes",
        "decide_timeout_minutes",
        "slack",
    }
    if unknown:
        raise ConfigError(
            f"{SECTION}: unknown key(s) {', '.join(sorted(map(str, unknown)))}"
        )
    if "ack_timeout_minutes" in section:
        cfg.ack_timeout_minutes = _minutes(
            section["ack_timeout_minutes"], "ack_timeout_minutes"
        )
    if "decide_timeout_minutes" in section:
        cfg.decide_timeout_minutes = _minutes(
            section["decide_timeout_minutes"], "decide_timeout_minutes"
        )
    reviewers = section.get("reviewers") or {}
    if not isinstance(reviewers, Mapping):
        raise ConfigError(
            f"{SECTION}.reviewers must map pm and engineer to lists of names"
        )
    for role, names in reviewers.items():
        if role not in ROLES:
            raise ConfigError(f"{SECTION}.reviewers: unknown role {role!r}")
        if not isinstance(names, list) or not all(
            isinstance(n, str) and n for n in names
        ):
            raise ConfigError(f"{SECTION}.reviewers.{role} must be a list of names")
        cfg.reviewers[role] = list(names)
    stops = section.get("stops") or {}
    if not isinstance(stops, Mapping):
        raise ConfigError(f"{SECTION}.stops must map stop numbers 1-5 to settings")
    for key, value in stops.items():
        try:
            n = int(key)
        except (TypeError, ValueError):
            n = 0
        if n not in DEFAULT_STOP_ROLES:
            raise ConfigError(f"{SECTION}.stops: {key!r} is not a stop number 1-5")
        value = value or {}
        if not isinstance(value, Mapping) or set(value) - {
            "reviewers",
            "ack_timeout_minutes",
            "decide_timeout_minutes",
        }:
            raise ConfigError(
                f"{SECTION}.stops.{n} takes reviewers, ack_timeout_minutes, decide_timeout_minutes"
            )
        cfg.stops[n] = StopConfig(
            roles=_roles(value["reviewers"], f"stops.{n}.reviewers")
            if "reviewers" in value
            else DEFAULT_STOP_ROLES[n],
            ack_timeout_minutes=_minutes(
                value["ack_timeout_minutes"], f"stops.{n}.ack_timeout_minutes"
            )
            if "ack_timeout_minutes" in value
            else cfg.ack_timeout_minutes,
            decide_timeout_minutes=_minutes(
                value["decide_timeout_minutes"], f"stops.{n}.decide_timeout_minutes"
            )
            if "decide_timeout_minutes" in value
            else cfg.decide_timeout_minutes,
        )
    slack = section.get("slack")
    if slack is not None:
        if not isinstance(slack, Mapping) or not isinstance(slack.get("channel"), str):
            raise ConfigError(
                f"{SECTION}.slack must be {{channel: <channel id or name>}}"
            )
        cfg.slack_channel = slack["channel"]
    return cfg


def load(repo: Path) -> ViewerConfig:
    path = Path(repo) / CONFIG_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ViewerConfig()
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    if not isinstance(data, Mapping):
        raise ConfigError(f"{path} must hold a mapping")
    return parse(data.get(SECTION))
