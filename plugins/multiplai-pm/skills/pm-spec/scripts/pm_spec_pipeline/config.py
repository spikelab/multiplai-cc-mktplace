"""One run's settings: where the change lives, and which model each step runs on.

Models and efforts come from `specs/config.yaml`, under `models:` and
`efforts:`. The keys are a step (`interview`, `spec_artifact`, `epic_split`,
`consistency`, `component_check`, `prototype`), a stage (`spec`, `agent`) or `default`, so a
config written for buildme keeps working: buildme's `interview` step and `spec`
stage, and its `prototype` step in the `agent` stage, mean the same here. The
chain for one step, first set link wins: `models.<step>`, `models.<stage>`,
`models.default`, then multiplai-core's `default_model()` (None leaves the
choice to the CLI).

`interview:` (`self` | `user` | `ticket`) and `prototype: {enabled: auto |
true | false}` are read from the same file; a command-line flag wins.
`consistency: {max_rounds: N}` caps the blocked checks after the specs review.
`component_check: {max_parallel: N}` is how many component checks run at once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from multiplai_core import resolve_model_spec
from multiplai_core.specs import archived_change_dirs, normalize_change_name

if TYPE_CHECKING:
    from .branch import Place

log = logging.getLogger(__name__)

STEP_STAGES: dict[str, str] = {
    "interview": "spec",
    "spec_artifact": "spec",
    "epic_split": "spec",
    "consistency": "spec",
    "component_check": "spec",
    "prototype": "agent",
}

INTERVIEW_MODES = ("self", "user", "ticket")
PROTOTYPE_MODES = ("auto", "true", "false")

RUN_DIR = ".pm-spec"
DEFAULT_MAX_ROUNDS = 5
DEFAULT_MAX_PARALLEL = 4


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
    # The archived change dir `revise` works on when specs/changes/<name>/ is
    # gone (`use_archive_if_gone`); None: the change dir is specs/changes/<name>/.
    archived: Path | None = None
    # The feature branch and the checkout the run writes in (`branch.place`);
    # None: nothing is pushed (a config built without the command line).
    place: Place | None = None

    def __post_init__(self) -> None:
        self.project_dir = Path(self.project_dir).expanduser().resolve()
        self.change_name = normalize_change_name(self.change_name)
        if not self.change_name:
            raise ValueError("a change name is required")
        self.specs_dir = (
            Path(self.specs_dir) if self.specs_dir else self.project_dir / "specs"
        )
        if not self.data:
            self.data = read_specs_config(self.specs_dir)

    @property
    def change_dir(self) -> Path:
        if self.archived is not None:
            return self.archived
        return self.specs_dir / "changes" / self.change_name

    def use_archive_if_gone(self) -> None:
        """Work on the change's newest archived dir (`specs/archive/<date>-<name>/`,
        `archived_change_dirs`) when specs/changes/<name>/ does not exist: an
        `--auto` build archives the change, and buildme-findings writes later
        requirements issues into that copy."""
        if not (self.specs_dir / "changes" / self.change_name).is_dir():
            dirs = archived_change_dirs(self.specs_dir, self.change_name)
            if dirs:
                self.archived = dirs[-1]

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

    @property
    def consistency_max_rounds(self) -> int:
        """`consistency: {max_rounds: N}`: the checks after the specs review that
        may block before `resume` stops checking (default 5)."""
        value = self._table("consistency").get("max_rounds")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return DEFAULT_MAX_ROUNDS

    @property
    def component_check_max_parallel(self) -> int:
        """`component_check: {max_parallel: N}`: the component checks that run
        at once (default 4)."""
        value = self._table("component_check").get("max_parallel")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return DEFAULT_MAX_PARALLEL

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
