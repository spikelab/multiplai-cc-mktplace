"""E2E listing of subcommands run through their program's launchers and through typer apps.

A console script or `python -m` that runs a CLI program reaches every
subcommand of that program, and a typer app reaches its commands as typer
dispatches them: a single-command app runs its command with no command name,
and a callback runs whenever its app does.
"""

from __future__ import annotations

from conftest import line_of
from test_e2e_listing import BASE, ids, listing, rec, snapshot, src

PYPROJECT = src("""
    [project]
    name = "x"
    version = "0"

    [project.scripts]
    mytool = "pkg.cli:tool_main"
    prog = "pkg.__main__:main"
    clicktool = "app.cmd:cli"
    ttool = "tapp.cli:app"
    """)

CLICK_MAIN = "from app.cmd import cli\n\ncli()\n"

TYPER = src("""
    import typer

    app = typer.Typer()
    users = typer.Typer()
    app.add_typer(users, name="users")


    @app.callback()
    def main():
        pass


    @app.command()
    def sync():
        pass


    @users.command()
    def add_user():
        pass


    single = typer.Typer()


    @single.command()
    def hello(name: str = ""):
        pass
    """)

LAUNCHERS = {
    **BASE,
    "pyproject.toml": PYPROJECT,
    "app/__main__.py": CLICK_MAIN,
    "tapp/__init__.py": "",
    "tapp/cli.py": TYPER,
}
TYPER_COMMANDS = ("main", "sync", "users add-user", "hello")


def subprocess_test(command: str) -> str:
    return f"import subprocess\n\n\ndef test_run():\n    subprocess.run({command})\n"


def runner_test(app: str, args: str) -> str:
    return src(f"""
        from typer.testing import CliRunner

        from tapp.cli import app, single, users


        def test_run():
            CliRunner().invoke({app}, {args})
        """)


def test_a_console_script_running_an_argparse_main_reaches_the_named_subcommand(
    tmp_path,
):
    body = subprocess_test('["prog", "open", "--draft"]')
    out, _ = listing(
        snapshot(tmp_path, {"tests/test_prog.py": body}, base=LAUNCHERS),
        "open",
        "render",
        "mytool",
    )
    record = rec(out["open"], "tests/test_prog.py::test_run")
    assert (record.match, record.invocation_line) == (
        "subprocess",
        line_of(body, "subprocess.run"),
    )
    assert out["render"].e2e == [] and out["mytool"].e2e == []


def test_a_console_script_command_string_reaches_the_subcommand(tmp_path):
    body = subprocess_test('"prog render", shell=True')
    out, _ = listing(
        snapshot(tmp_path, {"tests/test_prog.py": body}, base=LAUNCHERS),
        "open",
        "render",
    )
    assert (ids(out["render"]), ids(out["open"])) == (
        ["tests/test_prog.py::test_run"],
        [],
    )


def test_a_console_script_of_another_function_does_not_reach_the_subcommand(tmp_path):
    body = subprocess_test('["mytool", "open"]')
    out, _ = listing(
        snapshot(tmp_path, {"tests/test_tool.py": body}, base=LAUNCHERS),
        "open",
        "mytool",
    )
    assert out["open"].e2e == []
    assert ids(out["mytool"]) == ["tests/test_tool.py::test_run"]


def test_a_console_script_running_a_click_group_reaches_its_command(tmp_path):
    body = subprocess_test('["clicktool", "sync"]')
    out, _ = listing(
        snapshot(tmp_path, {"tests/test_click.py": body}, base=LAUNCHERS),
        "cli sync",
        "cli push",
    )
    assert [(t.node_id, t.match) for t in out["cli sync"].e2e] == [
        ("tests/test_click.py::test_run", "subprocess")
    ]
    assert out["cli push"].e2e == []


def test_python_m_of_a_package_calling_a_click_group_reaches_its_command(tmp_path):
    body = (
        "import subprocess\nimport sys\n\n\ndef test_run():\n"
        '    subprocess.run([sys.executable, "-m", "app", "push"])\n'
    )
    out, _ = listing(
        snapshot(tmp_path, {"tests/test_click.py": body}, base=LAUNCHERS),
        "cli sync",
        "cli push",
    )
    assert (ids(out["cli push"]), ids(out["cli sync"])) == (
        ["tests/test_click.py::test_run"],
        [],
    )


def test_a_console_script_naming_a_typer_app_reaches_its_mounted_command(tmp_path):
    body = subprocess_test('["ttool", "users", "add-user"]')
    out, _ = listing(
        snapshot(tmp_path, {"tests/test_t.py": body}, base=LAUNCHERS), *TYPER_COMMANDS
    )
    assert {name: ids(listed) for name, listed in out.items()} == {
        "main": ["tests/test_t.py::test_run"],
        "sync": [],
        "users add-user": ["tests/test_t.py::test_run"],
        "hello": [],
    }


def test_a_single_command_typer_app_runs_its_command_without_the_command_name(tmp_path):
    body = runner_test("single", '["--name", "x"]')
    out, _ = listing(
        snapshot(tmp_path, {"tests/test_t.py": body}, base=LAUNCHERS), *TYPER_COMMANDS
    )
    record = rec(out["hello"], "tests/test_t.py::test_run")
    assert (record.match, record.invocation_line) == (
        "cli_runner",
        line_of(body, "invoke("),
    )
    assert [name for name, listed in out.items() if listed.e2e] == ["hello"]


def test_a_typer_app_reaches_its_mounted_commands_and_callback_but_not_another_apps(
    tmp_path,
):
    body = runner_test("app", '["users", "add-user"]')
    out, _ = listing(
        snapshot(tmp_path, {"tests/test_t.py": body}, base=LAUNCHERS), *TYPER_COMMANDS
    )
    assert [name for name, listed in out.items() if listed.e2e] == [
        "main",
        "users add-user",
    ]


def test_a_mounted_typer_app_invoked_directly_runs_its_single_command(tmp_path):
    body = runner_test("users", "[]")
    out, _ = listing(
        snapshot(tmp_path, {"tests/test_t.py": body}, base=LAUNCHERS), *TYPER_COMMANDS
    )
    assert [name for name, listed in out.items() if listed.e2e] == ["users add-user"]


def test_a_command_name_passed_to_an_unrelated_typer_app_does_not_reach_that_command(
    tmp_path,
):
    body = runner_test("single", '["sync"]')
    out, _ = listing(
        snapshot(tmp_path, {"tests/test_t.py": body}, base=LAUNCHERS), *TYPER_COMMANDS
    )
    assert [name for name, listed in out.items() if listed.e2e] == ["hello"]
