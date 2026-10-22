"""`stop_record.others_work` for a process that cannot import buildme.

Skills do not import each other in an installed plugin, so another skill
(spec-extract) runs this module as a subprocess:

    uv run --directory <skills>/buildme/scripts python -m build_pipeline.stop_record_cli

It reads one JSON object on stdin:

    {"project_dir": "/abs/path", "stop": null | {"head": ..., "edits": {...}},
     "ignore": null | "dir", "head": null | "<sha>", "origin_ref": null | "<ref>"}

`project_dir` is required and must be absolute, because `uv run --directory`
runs this module from buildme's own scripts directory. `stop` is validated as
`stop_record.StepStop`; the other keys are optional and default to null. It
prints one line, `{"reason": null}` when the caller may go on, or
`{"reason": "<text>"}` when it must not, and exits 0. Malformed input exits 2
with the error on stderr and nothing on stdout.

The rules are `others_work`'s, unchanged: this module adds no keyword to it
and lists no changes itself.
"""

from __future__ import annotations

import json
import os
import sys

from . import stop_record

_OPTIONAL = ("ignore", "head", "origin_ref")
_KEYS = {"project_dir", "stop", *_OPTIONAL}


class _BadInput(Exception):
    pass


def _parse(text: str) -> tuple[str, stop_record.StepStop | None, dict[str, str | None]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise _BadInput(f"stdin is not JSON: {e}") from None
    if not isinstance(data, dict):
        raise _BadInput("stdin must be one JSON object")
    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise _BadInput(f"unknown keys: {', '.join(unknown)}")
    project_dir = data.get("project_dir")
    if not isinstance(project_dir, str) or not project_dir:
        raise _BadInput("project_dir must be a non-empty string")
    if not os.path.isabs(project_dir):
        raise _BadInput(f"project_dir must be an absolute path, not {project_dir!r}")
    options: dict[str, str | None] = {}
    for key in _OPTIONAL:
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise _BadInput(f"{key} must be a string or null")
        options[key] = value
    stop = None
    if data.get("stop") is not None:
        try:
            stop = stop_record.StepStop.model_validate(data["stop"])
        except ValueError as e:  # pydantic's ValidationError is a ValueError
            raise _BadInput(f"stop is not a stop record: {e}") from None
    return project_dir, stop, options


def main() -> int:
    try:
        project_dir, stop, options = _parse(sys.stdin.read())
    except _BadInput as e:
        print(f"stop_record_cli: {e}", file=sys.stderr)
        return 2
    reason = stop_record.others_work(
        project_dir,
        stop,
        ignore=options["ignore"],
        head=options["head"],
        origin_ref=options["origin_ref"],
    )
    print(json.dumps({"reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
