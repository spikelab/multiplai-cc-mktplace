"""The signatures section of the test writer's context: read with `ast` from the
working tree, with no model call."""

import subprocess
from pathlib import Path

import pytest

from build_pipeline import signatures
from build_pipeline.config import BuildConfig
from build_pipeline.models import BlockInfo
from build_pipeline.tdd_engine import assemble_context

TRACE = '''\
"""Trace each path through the code.

More detail that stays out of the section.
"""
from dataclasses import dataclass

LIMIT = 40


@dataclass
class Step:
    """One step of a path."""

    name: str
    line: int = 0

    def render(self, width: int = 80) -> str:
        """The step as one line."""
        return self.name

    def _hidden(self):
        return None


def trace(paths: list[str], *, depth: int = 2) -> list[Step]:
    """Trace every path."""
    body_marker_do_not_show = 1
    return []


def _private():
    return None
'''

OTHER = '''\
def helper_used_by_consumes(x: int) -> int:
    """Defined once in the project."""
    return x
'''

CONFTEST = '''\
import pytest


@pytest.fixture
def tracer(tmp_path):
    """A tracer over a temp dir."""
    return tmp_path
'''

FAKES = '''\
class FakeRepo:
    """A repo that answers from memory."""

    def files(self) -> list[str]:
        return []


def make_step(name="a"):
    return name
'''

TEST_SHORT = '''\
from pkg.stages.trace import trace


def test_trace_of_nothing_is_empty():
    assert trace([]) == []
'''

TEST_LONG = '''\
from pkg.stages import trace as tr


class TestStep:
    def test_render(self):
        assert tr.Step("a").render() == "a"

    def test_unrelated_method(self):
        assert 1 + 1 == 2


def test_padding_one():
    assert True is not False


def test_padding_two():
    assert [] == []
'''

TEST_UNRELATED = '''\
def test_x():
    assert 1 == 1
'''

FILES = {
    "pyproject.toml": "[project]\nname = 'pkg'\n",
    "pkg/__init__.py": "",
    "pkg/stages/__init__.py": "",
    "pkg/stages/trace.py": TRACE,
    "pkg/other.py": OTHER,
    "pkg/tests/__init__.py": "",
    "pkg/tests/conftest.py": CONFTEST,
    "pkg/tests/fakes.py": FAKES,
    "pkg/tests/test_trace.py": TEST_SHORT,
    "pkg/tests/test_long.py": TEST_LONG,
    "pkg/tests/test_unrelated.py": TEST_UNRELATED,
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return path


@pytest.fixture
def project(tmp_path):
    return _repo(tmp_path / "proj", FILES)


def _block(description="Edit `pkg/stages/trace.py` so `trace` follows calls.", consumes=()) -> BlockInfo:
    return BlockInfo(number=1, name="Trace calls", description=description, consumes=list(consumes))


def _part(section: str, heading: str) -> str:
    """The text under *heading* (a `### ` line), up to the next `### ` heading."""
    after = section.split(heading + "\n", 1)[1]
    return after.split("\n### ", 1)[0]


class TestModules:
    def test_a_named_module_gets_its_public_signatures_and_no_bodies(self, project):
        section = signatures.signatures_section(_block(), project)
        assert section.startswith(signatures.HEADING)
        assert signatures.INTRO in section
        part = _part(section, "### `pkg/stages/trace.py`")
        assert "Trace each path through the code." in part
        assert "More detail" not in part
        assert "LIMIT = 40" in part
        assert "@dataclass\nclass Step:  # One step of a path." in part
        assert "    name: str\n    line: int = 0" in part
        assert "    def render(self, width: int = 80) -> str  # The step as one line." in part
        assert "def trace(paths: list[str], *, depth: int = 2) -> list[Step]  # Trace every path." in part
        for hidden in ("body_marker_do_not_show", "_hidden", "_private", "return"):
            assert hidden not in part

    def test_a_partial_path_names_the_module(self, project):
        section = signatures.signatures_section(_block("Edit stages/trace.py."), project)
        assert "### `pkg/stages/trace.py`" in section

    def test_a_private_function_the_block_names_is_listed(self, project):
        section = signatures.signatures_section(_block("Edit `pkg/stages/trace.py`: `_private`."), project)
        assert "def _private()" in _part(section, "### `pkg/stages/trace.py`")

    def test_the_module_defining_a_consumed_name_is_added(self, project):
        block = _block(consumes=["helper_used_by_consumes(x: int) -> int"])
        section = signatures.signatures_section(block, project)
        assert "def helper_used_by_consumes(x: int) -> int  # Defined once in the project." in \
            _part(section, "### `pkg/other.py`")

    def test_the_module_defining_a_backticked_name_is_added(self, project):
        section = signatures.signatures_section(_block("Call `helper_used_by_consumes` from the CLI."), project)
        assert "### `pkg/other.py`" in section

    def test_a_backticked_name_defined_twice_adds_no_module(self, project):
        (project / "pkg" / "again.py").write_text(OTHER)
        section = signatures.signatures_section(_block("Call `helper_used_by_consumes`."), project)
        assert section == ""

    def test_files_are_read_from_the_working_tree_not_from_head(self, project):
        (project / "pkg" / "stages" / "trace.py").write_text(TRACE + "\n\ndef added_since(n: int) -> None:\n    pass\n")
        section = signatures.signatures_section(_block(), project)
        assert "def added_since(n: int) -> None" in section


class TestTestsDirectory:
    def test_conftest_fixtures_and_helper_files_are_listed(self, project):
        section = signatures.signatures_section(_block(), project)
        conftest = _part(section, "### `pkg/tests/conftest.py`")
        assert "@pytest.fixture\ndef tracer(tmp_path)  # A tracer over a temp dir." in conftest
        fakes = _part(section, "### `pkg/tests/fakes.py`")
        assert "class FakeRepo:  # A repo that answers from memory." in fakes
        assert "    def files(self) -> list[str]" in fakes
        assert "def make_step(name='a')" in fakes

    def test_the_tests_that_name_a_symbol_of_the_module_are_listed_as_node_ids(self, project):
        section = signatures.signatures_section(_block(), project)
        pins = section.split(signatures.PINS_HEADING, 1)[1].split("\n### ", 1)[0]
        assert "- `pkg/tests/test_trace.py::test_trace_of_nothing_is_empty`" in pins
        assert "- `pkg/tests/test_long.py::TestStep::test_render`" in pins
        assert "test_unrelated_method" not in pins
        assert "test_unrelated.py" not in pins

    def test_the_shortest_test_file_importing_the_module_is_given_verbatim(self, project):
        section = signatures.signatures_section(_block(), project)
        example = section.split(signatures.EXAMPLE_HEADING + " `pkg/tests/test_trace.py`", 1)[1]
        assert TEST_SHORT in example
        assert "class TestStep" not in example

    def test_the_example_is_cut_at_200_lines(self, project):
        long_test = "from pkg.stages.trace import trace\n" + "".join(
            f"\n\ndef test_{i}():\n    assert trace([]) == []\n" for i in range(100))
        for name in ("test_trace.py", "test_long.py"):
            (project / "pkg" / "tests" / name).write_text(long_test)
        section = signatures.signatures_section(_block(), project)
        example = section.split(signatures.EXAMPLE_HEADING, 1)[1]
        fence = example.split("```python\n", 1)[1].split("```", 1)[0]
        assert len(fence.splitlines()) == 200


class TestLimits:
    def test_the_section_stops_at_40000_characters_and_says_what_it_cut(self, project):
        big = "".join(f'def function_number_{i}(argument: int = {i}) -> int:\n    """Doc {i}."""\n    return {i}\n\n'
                      for i in range(3000))
        (project / "pkg" / "stages" / "trace.py").write_text(big)
        section = signatures.signatures_section(_block(), project)
        assert len(section) <= signatures.MAX_CHARS == 40_000
        assert "def function_number_0(argument: int = 0) -> int" in section
        assert "Cut to stay under 40,000 characters:" in section
        assert "entries of `pkg/stages/trace.py`" in section

    def test_a_non_python_project_gets_nothing(self, tmp_path):
        repo = _repo(tmp_path / "node", {"package.json": "{}", "src/trace.py": TRACE})
        assert signatures.signatures_section(_block("Edit `src/trace.py`."), repo) == ""

    def test_a_block_naming_no_module_gets_nothing(self, project):
        assert signatures.signatures_section(_block("Write the README."), project) == ""

    def test_a_file_that_does_not_parse_is_named_as_unread(self, project):
        (project / "pkg" / "stages" / "trace.py").write_text("def broken(:\n")
        section = signatures.signatures_section(_block(), project)
        assert "### `pkg/stages/trace.py`" in section
        assert "could not be parsed" in _part(section, "### `pkg/stages/trace.py`")


class TestContext:
    def _config(self, project: Path) -> BuildConfig:
        config = BuildConfig(project_dir=project, change_name="c", config_dir=project / "cfg",
                             core_memory_files=[], stack_memory_files=[], additional_memory_files=[])
        config.specs_dir = project / "specs"
        return config

    def test_only_the_test_writer_gets_the_section(self, project):
        config = self._config(project)
        assert signatures.HEADING in assemble_context(_block(), config, "test_writer")
        for role in ("implementer", "refactorer"):
            assert signatures.HEADING not in assemble_context(_block(), config, role)
