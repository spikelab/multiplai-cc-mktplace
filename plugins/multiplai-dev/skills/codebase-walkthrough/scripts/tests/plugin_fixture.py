"""A small plugin layout for `--scope codebase`, in its own workspace (never the shared `ws`).

    pws/
      kit/        git repo: plugins/demo with a `tool` skill (the target: plugins/demo/skills/tool)
      corelib/    git repo: the logging library `tool` calls (read with --lib)

`tool` has two subcommands, a loop capped by `MAX_TRIES = 3`, `sys.exit(2)`, a `DONE:` marker
and a state file it reads back on a re-run. Its docs are at three levels, so the survey finds
the skill's own SKILL.md and CLAUDE.md, and the plugin's README.md and CHANGELOG.md (the nearest
README.md, not the repository's).
"""

from __future__ import annotations

from pathlib import Path

from fixture_repo import make_repo

TOOL = "plugins/demo/skills/tool"

KIT = {
    "README.md": "# kit\n\nA repository of plugins.\n",
    "plugins/demo/README.md": "# demo\n\nThe demo plugin holds the `tool` skill.\n",
    "plugins/demo/CHANGELOG.md": (
        "# Changelog\n\n## [0.2.0]\n\n- `tool check` exits 2 when something is wrong.\n"
    ),
    f"{TOOL}/SKILL.md": (
        "---\n"
        "name: tool\n"
        "description: Builds things.\n"
        "---\n"
        "\n"
        "# Tool\n"
        "\n"
        "Use `tool build --run-dir <dir>` for a full build.\n"
        "Use `tool check` for a quick check; it exits 2 when something is wrong.\n"
        "\n"
        "| Command | When |\n"
        "|---|---|\n"
        "| `tool build` | a build |\n"
        "| `--run-dir <dir>` | where the build keeps tool-state.json |\n"
        "Re-running `tool build` keeps the attempts recorded in tool-state.json.\n"
    ),
    f"{TOOL}/scripts/CLAUDE.md": "# tool scripts\n\nRun `python -m tool build`.\n",
    f"{TOOL}/scripts/tool/__init__.py": "",
    f"{TOOL}/scripts/tool/__main__.py": (
        "import argparse\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "from corelib.log import log_line\n"
        "\n"
        "MAX_TRIES = 3\n"
        'STATE = "tool-state.json"\n'
        "\n"
        "\n"
        "def load_state(run_dir):\n"
        "    path = Path(run_dir) / STATE\n"
        "    if path.exists():\n"
        "        return json.loads(path.read_text())\n"
        '    return {"done": []}\n'
        "\n"
        "\n"
        "def save_state(run_dir, state):\n"
        "    (Path(run_dir) / STATE).write_text(json.dumps(state))\n"
        "\n"
        "\n"
        "def build(run_dir):\n"
        "    state = load_state(run_dir)\n"
        "    for attempt in range(MAX_TRIES):\n"
        '        log_line(run_dir, f"attempt {attempt}")\n'
        '        if attempt in state["done"]:\n'
        "            continue\n"
        '        state["done"].append(attempt)\n'
        "        save_state(run_dir, state)\n"
        '    print("DONE: built")\n'
        "    return 0\n"
        "\n"
        "\n"
        "def main(argv=None):\n"
        '    parser = argparse.ArgumentParser(prog="tool")\n'
        '    sub = parser.add_subparsers(dest="command", required=True)\n'
        '    b = sub.add_parser("build")\n'
        '    b.add_argument("--run-dir", default=".")\n'
        '    sub.add_parser("check")\n'
        "    args = parser.parse_args(argv)\n"
        '    if args.command == "check":\n'
        "        sys.exit(2)\n"
        "    return build(args.run_dir)\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    sys.exit(main())\n"
    ),
    f"{TOOL}/scripts/tests/test_tool.py": (
        "from tool.__main__ import build\n"
        "\n"
        "\n"
        "def test_build(tmp_path):\n"
        "    assert build(tmp_path) == 0\n"
    ),
}

CORELIB = {
    "corelib/__init__.py": "",
    "corelib/log.py": (
        "from pathlib import Path\n"
        "\n"
        'LOG_NAME = "tool.log"\n'
        "\n"
        "\n"
        "def log_line(run_dir, text):\n"
        '    with (Path(run_dir) / LOG_NAME).open("a") as f:\n'
        '        f.write(text + "\\n")\n'
    ),
}


def build(ws: Path) -> dict[str, Path]:
    """Create the plugin workspace under *ws*; return {name: path}."""
    ws.mkdir(parents=True, exist_ok=True)
    paths = {
        "ws": ws,
        "kit": ws / "kit",
        "corelib": ws / "corelib",
        "tool": ws / "kit" / TOOL,
    }
    make_repo(paths["kit"], KIT)
    make_repo(paths["corelib"], CORELIB)
    return paths
