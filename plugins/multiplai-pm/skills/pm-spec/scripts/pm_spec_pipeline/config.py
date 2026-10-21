"""One run's settings: where the change lives, and which model each step runs on.

Models and efforts come from `specs/config.yaml`, under `models:` and
`efforts:`. The keys are a step (`interview`, `spec_artifact`, `epic_split`,
`consistency`, `prototype`), a stage (`spec`, `agent`) or `default`, so a
config written for buildme keeps working: buildme's `interview` step and `spec`
stage, and its `prototype` step in the `agent` stage, mean the same here. The
chain for one step, first set link wins: `models.<step>`, `models.<stage>`,
`models.default`, then multiplai-core's `default_model()` (None leaves the
choice to the CLI).

`interview:` (`self` | `user` | `ticket`) and `prototype: {enabled: auto |
true | false}` are read from the same file; a command-line flag wins.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from multiplai_core import resolve_model_spec
from multiplai_core.specs import normalize_change_name

log = logging.getLogger(__name__)

STEP_STAGES: dict[str, str] = {
    "interview": "spec",
    "spec_artifact": "spec",
    "epic_split": "spec",
    "consistency": "spec",
    "prototype": "agent",
}

INTERVIEW_MODES = ("self", "user", "ticket")
PROTOTYPE_MODES = ("auto", "true", "false")

RUN_DIR = ".pm-spec"


def read_specs_config(specs_dir: Path) -> dict:
    """The mapping in `<specs_dir>/config.yaml`; `{}` when it is missing or not a mapping."""
    path = Path(specs_dir) / "config.yaml"
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("Cannot read %s, using defaults: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class SpecConfig:
    project_dir: Path
    change_name: str
    specs_dir: Path | None = None
    auto: bool = False
    data: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.project_dir = Path(self.project_dir).expanduser().resolve()
        self.change_name = normalize_change_name(self.change_name)
        if not self.change_name:
            raise ValueError("a change name is required")
        self.specs_dir = Path(self.specs_dir) if self.specs_dir else self.project_dir / "specs"
        if not self.data:
            self.data = read_specs_config(self.specs_dir)

    @property
    def change_dir(self) -> Path:
        return self.specs_dir / "changes" / self.change_name

    @property
    def run_dir(self) -> Path:
        return self.change_dir / RUN_DIR

    def change_dir_for(self, name: str) -> Path:
        return self.specs_dir / "changes" / normalize_change_name(name)

    @property
    def project_context(self) -> str:
        value = self.data.get("context", "")
        return value if isinstance(value, str) else ""

    def interview_mode(self, flag: str = "") -> str:
        mode = flag or str(self.data.get("interview") or "").strip().lower()
        if mode in INTERVIEW_MODES:
            return mode
        return "self" if self.auto else "user"

    def prototype_mode(self, flag: str = "") -> str:
        if flag:
            return flag
        proto = self.data.get("prototype")
        value = proto.get("enabled") if isinstance(proto, dict) else None
        text = str(value).strip().lower() if value is not None else "auto"
        return text if text in PROTOTYPE_MODES else "auto"

    def _table(self, key: str) -> dict:
        value = self.data.get(key)
        return value if isinstance(value, dict) else {}

    def model_for(self, step: str) -> str | None:
        """The model *step* runs on: a bare Claude ID, a `pi:` spec, or None (the session's)."""
        stage = STEP_STAGES[step]
        models = self._table("models")
        chain = [models.get(step), models.get(stage), models.get("default")]
        spec = resolve_model_spec([c if isinstance(c, str) else None for c in chain])
        if spec is None:
            from multiplai_core.session import default_model
            return default_model()
        return spec.model if spec.is_anthropic else spec.qualified

    def effort_for(self, step: str) -> str | None:
        """The effort *step* runs at, by the same chain as `model_for`; None means the default."""
        stage = STEP_STAGES[step]
        efforts = self._table("efforts")
        for key in (step, stage, "default"):
            value = efforts.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None
