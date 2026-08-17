"""Dev reference injection: stack detection, pointer block, announce state.

The behaviour under test is that engineering standards load because of what
the project IS — no router, no prompt wording — and that the mechanism is
silent on a machine that has no reference/dev directory.
"""

import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import reference_docs  # noqa: E402


@pytest.fixture
def ref_dir(tmp_path, monkeypatch):
    """A CLAUDE_CONFIG_DIR whose reference/dev holds the real doc names."""
    config_dir = tmp_path / "claude-config"
    docs = config_dir / "reference" / "dev"
    docs.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    return docs


def write_doc(docs: Path, name: str, sections: list[str]) -> Path:
    body = "\n\n".join(f"## {s}\nbody of {s}\n" for s in sections)
    path = docs / name
    path.write_text(f"# {name}\n\nintro\n\n{body}")
    return path


class TestStackDetection:
    def test_manage_py_detects_django_on_top_of_pyproject(self, tmp_path):
        project = tmp_path / "site"
        project.mkdir()
        (project / "pyproject.toml").write_text('[project]\nname = "site"\n')
        (project / "manage.py").write_text("")
        assert reference_docs.detect_stack_keys(project) == ["pyproject", "django"]

    def test_django_read_out_of_requirements_txt(self, tmp_path):
        project = tmp_path / "site"
        project.mkdir()
        (project / "requirements.txt").write_text("# deps\nDjango[argon2]==5.2\nrequests\n")
        assert "django" in reference_docs.detect_stack_keys(project)

    def test_fastapi_and_plain_library_are_distinguished(self, tmp_path):
        api, lib = tmp_path / "api", tmp_path / "lib"
        for p in (api, lib):
            p.mkdir()
        (api / "pyproject.toml").write_text(
            '[project]\nname = "api"\ndependencies = ["fastapi"]\n'
        )
        (lib / "pyproject.toml").write_text(
            '[project]\nname = "lib"\ndependencies = ["pydantic"]\n'
        )
        assert reference_docs.detect_stack_keys(api) == ["pyproject", "fastapi"]
        assert reference_docs.detect_stack_keys(lib) == ["pyproject"]

    def test_next_only_dependency_still_counts_as_react(self, tmp_path):
        project = tmp_path / "web"
        project.mkdir()
        (project / "package.json").write_text('{"dependencies": {"next": "^15"}}')
        assert reference_docs.detect_stack_keys(project) == ["package", "react"]

    def test_malformed_manifests_cost_a_hint_not_the_turn(self, tmp_path):
        project = tmp_path / "broken"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project\nnot toml")
        (project / "package.json").write_text("{ not json")
        # Manifest *filenames* still register; only the framework hints are lost.
        assert reference_docs.detect_stack_keys(project) == ["pyproject", "package"]


class TestProjectResolution:
    def test_walks_up_to_the_nearest_manifest(self, tmp_path):
        project = tmp_path / "repo"
        deep = project / "src" / "app" / "views"
        deep.mkdir(parents=True)
        (project / "pyproject.toml").write_text('[project]\nname = "x"\n')
        assert reference_docs.find_project_dir(deep) == project.resolve()

    def test_workspace_root_with_no_manifest_resolves_nothing(self, tmp_path):
        root = tmp_path / "workspace"
        (root / "PROJECTS" / "thing").mkdir(parents=True)
        assert reference_docs.find_project_dir(root) is None

    def test_prompt_path_finds_the_project_the_workspace_root_cannot(self, tmp_path):
        """The knowhere shape: cwd is a workspace root holding many repos and
        carrying no manifest, so the project has to come from the prompt."""
        root = tmp_path / "workspace"
        project = root / "PROJECTS" / "site"
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text('[project]\nname = "site"\n')
        found = reference_docs.projects_from_prompt(
            "fix the serializer in PROJECTS/site/api/views.py please", root,
        )
        assert found == [project.resolve()]

    def test_prompt_tokens_naming_nothing_are_dropped(self, tmp_path):
        root = tmp_path / "workspace"
        root.mkdir()
        assert reference_docs.projects_from_prompt("see https://x.dev/a/b", root) == []


class TestBlock:
    def test_block_names_paths_and_sections_not_contents(self, ref_dir, tmp_path):
        write_doc(ref_dir, "django-drf-best-practices.md", ["Layout", "Migrations"])
        project = tmp_path / "site"
        project.mkdir()
        docs = reference_docs.resolve_docs(["django-drf-best-practices.md"])
        block = reference_docs.build_block(project, docs)
        assert "=== DEV REFERENCES ===" in block
        assert "django-drf-best-practices.md" in block
        assert "Layout · Migrations" in block
        # Pointers only — the doc body must not be inlined.
        assert "body of Layout" not in block

    def test_section_index_is_capped_with_a_remainder_marker(self):
        text = "\n".join(f"## S{i}\ntext" for i in range(30))
        index = reference_docs.section_index(text, limit=5)
        assert index[:5] == ["S0", "S1", "S2", "S3", "S4"]
        assert index[-1] == "(+25 more)"

    def test_no_docs_means_no_block(self, tmp_path):
        assert reference_docs.build_block(tmp_path, []) == ""

    def test_missing_doc_is_skipped_and_present_one_kept(self, ref_dir):
        write_doc(ref_dir, "uv-python-best-practices.md", ["Layout"])
        docs = reference_docs.resolve_docs(
            ["uv-python-best-practices.md", "never-written.md"]
        )
        assert [p.name for p, _ in docs] == ["uv-python-best-practices.md"]


class TestAnnounceState:
    def test_first_sight_announces_then_stays_quiet(self, tmp_path):
        state: dict = {}
        project = tmp_path / "site"
        assert reference_docs.should_announce(state, project, 1)
        reference_docs.record_announced(state, project, 1)
        assert not reference_docs.should_announce(state, project, 2)

    def test_reannounces_after_the_window(self, tmp_path):
        state: dict = {}
        project = tmp_path / "site"
        reference_docs.record_announced(state, project, 1)
        window = reference_docs.REANNOUNCE_AFTER_TURNS
        assert not reference_docs.should_announce(state, project, 1 + window)
        assert reference_docs.should_announce(state, project, 2 + window)

    def test_a_second_project_gets_its_own_announcement(self, tmp_path):
        state: dict = {}
        reference_docs.record_announced(state, tmp_path / "a", 1)
        assert reference_docs.should_announce(state, tmp_path / "b", 2)

    def test_corrupt_state_reads_as_never_announced(self, tmp_path):
        assert reference_docs.should_announce({"dev_references": "junk"}, tmp_path, 5)

    def test_no_turn_counter_means_once_per_session(self, tmp_path):
        """With the cooldown off there is no turn index; the block must still
        announce exactly once rather than every prompt."""
        state: dict = {}
        project = tmp_path / "site"
        reference_docs.record_announced(state, project, 0)
        assert not reference_docs.should_announce(state, project, 0)


class TestDegradation:
    def test_no_reference_dir_means_no_reference_dir(self, tmp_path, monkeypatch):
        """Vanilla Claude Code: the docs ship with multiplai-kit, so a machine
        without it must see nothing at all — not a warning, not an error."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
        assert not reference_docs.reference_dir().is_dir()
        assert reference_docs.resolve_docs(["uv-python-best-practices.md"]) == []

    def test_an_unmapped_key_yields_no_names(self):
        assert reference_docs.doc_names_for(["nosuchstack"]) == []

    def test_doc_names_are_deduped_across_keys(self):
        names = reference_docs.doc_names_for(["pyproject", "pyproject", "django"])
        assert names == [
            "uv-python-best-practices.md",
            "python-project-structure.md",
            "python-review.md",
            "valid-patterns.md",
            "django-drf-best-practices.md",
        ]


class TestHookIntegration:
    """The block must reach the hook's stdout, and must be able to do so when
    no other corpus injected anything."""

    def _run_hook(self, tmp_path, prompt, cwd):
        import subprocess

        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude-config")
        env["CLAUDE_PLUGIN_OPTION_WORKSPACE_DIR"] = str(tmp_path / "ws")
        env["CLAUDE_PLUGIN_OPTION_RECOMMEND_COOLDOWN_TURNS"] = "0"
        payload = json.dumps({"prompt": prompt, "cwd": str(cwd), "session_id": "t"})
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "context_manager.py")],
            input=payload, capture_output=True, text=True, env=env, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout or "{}")

    @staticmethod
    def _injected(out: dict) -> str:
        """The text Claude Code actually receives, wherever the hook put it."""
        hook_specific = out.get("hookSpecificOutput") or {}
        return hook_specific.get("additionalContext") or ""

    def test_hook_emits_the_block_for_a_detected_project(self, tmp_path):
        docs = tmp_path / "claude-config" / "reference" / "dev"
        docs.mkdir(parents=True)
        write_doc(docs, "django-drf-best-practices.md", ["Migrations"])
        write_doc(docs, "uv-python-best-practices.md", ["Locking"])
        write_doc(docs, "python-project-structure.md", ["Layout"])
        project = tmp_path / "site"
        project.mkdir()
        (project / "pyproject.toml").write_text('[project]\nname = "site"\n')
        (project / "manage.py").write_text("")

        out = self._run_hook(tmp_path, "add an endpoint", project)
        injected = self._injected(out)
        assert "=== DEV REFERENCES ===" in injected
        assert "django-drf-best-practices.md" in injected

    def test_hook_stays_silent_without_a_reference_dir(self, tmp_path):
        project = tmp_path / "site"
        project.mkdir()
        (project / "pyproject.toml").write_text('[project]\nname = "site"\n')
        out = self._run_hook(tmp_path, "add an endpoint", project)
        assert "DEV REFERENCES" not in self._injected(out)


class TestSiblingReferenceDirs:
    """Issue #204: `dev` was hardcoded, so anything the kit shipped beside it —
    `reference/review/`, the six per-language checklists from multiplai-kit#57 —
    was invisible to the per-session pointer block. Invisibly so: a name that
    resolves nowhere is skipped with a log line, not an error."""

    def _refs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        return tmp_path / "reference"

    def test_dev_comes_first_and_review_follows(self, tmp_path, monkeypatch):
        root = self._refs(tmp_path, monkeypatch)
        for name in ("review", "dev"):
            (root / name).mkdir(parents=True)
        dirs = reference_docs.reference_dirs()
        assert [d.name for d in dirs] == ["dev", "review"]

    def test_precedence_is_the_allowlist_order_not_alphabetical(self, tmp_path, monkeypatch):
        """`dev` sorts after `archive` and before `review`; only the declared
        order may decide which copy of a name wins."""
        root = self._refs(tmp_path, monkeypatch)
        for name in ("archive", "dev", "review"):
            (root / name).mkdir(parents=True)
        assert [d.name for d in reference_docs.reference_dirs()] == ["dev", "review"]

    def test_an_unlisted_directory_is_not_searched(self, tmp_path, monkeypatch):
        """A stale copy parked in `reference/archive/` must not be injectable.

        Unfiltered, `archive` sorts ahead of `review`, so renaming the `dev`
        copy would silently promote the old one — the failure the renaming
        contract exists to prevent."""
        root = self._refs(tmp_path, monkeypatch)
        (root / "archive").mkdir(parents=True)
        (root / "review").mkdir(parents=True)
        (root / "archive" / "python-review.md").write_text("## Stale\n")
        (root / "review" / "python-review.md").write_text("## Current\n")

        resolved = reference_docs.resolve_docs(["python-review.md"])
        assert [p.parent.name for p, _ in resolved] == ["review"]
        assert resolved[0][1] == ["Current"]

    def test_a_hidden_directory_is_not_a_reference_dir(self, tmp_path, monkeypatch):
        """`reference/` as a bare git checkout has a `.git` and no docs. That
        must not read as "the kit is installed" — it sorts first AND makes the
        list non-empty, which passed the context_manager gate with nothing to
        resolve."""
        root = self._refs(tmp_path, monkeypatch)
        (root / ".git").mkdir(parents=True)
        assert reference_docs.reference_dirs() == []

    def test_missing_dev_does_not_hide_the_siblings(self, tmp_path, monkeypatch):
        root = self._refs(tmp_path, monkeypatch)
        (root / "review").mkdir(parents=True)
        assert [d.name for d in reference_docs.reference_dirs()] == ["review"]

    def test_no_reference_root_yields_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
        assert reference_docs.reference_dirs() == []

    def test_files_under_reference_are_not_directories(self, tmp_path, monkeypatch):
        root = self._refs(tmp_path, monkeypatch)
        root.mkdir(parents=True)
        (root / "README.md").write_text("# not a directory\n")
        assert reference_docs.reference_dirs() == []

    def test_a_doc_in_a_sibling_dir_now_resolves(self, tmp_path, monkeypatch):
        root = self._refs(tmp_path, monkeypatch)
        (root / "dev").mkdir(parents=True)
        (root / "review").mkdir(parents=True)
        (root / "review" / "go-review.md").write_text("## Errors\n\nbody\n")

        resolved = reference_docs.resolve_docs(["go-review.md"])
        assert len(resolved) == 1
        path, sections = resolved[0]
        assert path.parent.name == "review"
        assert sections == ["Errors"]

    def test_dev_wins_when_both_hold_the_name(self, tmp_path, monkeypatch):
        """`dev` first is what makes this change invisible to existing setups."""
        root = self._refs(tmp_path, monkeypatch)
        (root / "dev").mkdir(parents=True)
        (root / "review").mkdir(parents=True)
        (root / "dev" / "shared.md").write_text("## FromDev\n")
        (root / "review" / "shared.md").write_text("## FromReview\n")

        resolved = reference_docs.resolve_docs(["shared.md"])
        assert len(resolved) == 1, "a name in two directories must resolve once"
        assert resolved[0][0].parent.name == "dev"

    def test_a_name_in_no_directory_is_still_skipped_silently(self, tmp_path, monkeypatch):
        root = self._refs(tmp_path, monkeypatch)
        (root / "dev").mkdir(parents=True)
        (root / "review").mkdir(parents=True)
        assert reference_docs.resolve_docs(["nobody-wrote-this.md"]) == []

    def test_an_unreadable_copy_does_not_suppress_a_readable_sibling(
        self, tmp_path, monkeypatch, caplog,
    ):
        """"First hit wins" means first *readable* hit. A file that exists but
        cannot be read is not a hit, so it must fall through exactly as a
        missing file does — an unreadable `dev/` copy used to abandon the name
        entirely and hide the perfectly good `review/` one."""
        root = self._refs(tmp_path, monkeypatch)
        (root / "dev").mkdir(parents=True)
        (root / "review").mkdir(parents=True)
        broken = root / "dev" / "python-review.md"
        broken.write_text("## Unreadable\n")
        broken.chmod(0o000)
        if os.access(broken, os.R_OK):  # root, or a filesystem without modes
            pytest.skip("file permissions are not enforced for this user")
        (root / "review" / "python-review.md").write_text("## Readable\n")

        with caplog.at_level("WARNING", logger=reference_docs.logger.name):
            resolved = reference_docs.resolve_docs(["python-review.md"])
        assert [p.parent.name for p, _ in resolved] == ["review"]
        assert resolved[0][1] == ["Readable"]
        assert "unreadable" in caplog.text.lower()


class TestReviewChecklistsAreReachable:
    """Issue #204 needs the *map* widened too, not only the search.

    Every name `STACK_DOCS` could produce resolved under `dev/`, so on a real
    install the intersection with `reference/review/` was empty and the search
    change bought nothing. `go-review.md` was unreachable by construction:
    `STACK_DOCS["go"]` was `[]`, so `doc_names_for` returned nothing and
    `resolve_docs` was never called for a Go project at all.
    """

    REVIEW_DOCS = (
        "python-review.md",
        "javascript-review.md",
        "ios-review.md",
        "go-review.md",
        "valid-patterns.md",
    )

    @pytest.fixture
    def review_dir(self, tmp_path, monkeypatch):
        """A config dir whose reference/review holds the kit's real filenames."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        docs = tmp_path / "reference" / "review"
        docs.mkdir(parents=True)
        for name in self.REVIEW_DOCS:
            (docs / name).write_text("## Checks\n\nbody\n")
        return docs

    @pytest.mark.parametrize(
        "manifest,body,expected",
        [
            ("pyproject.toml", '[project]\nname = "p"\n', "python-review.md"),
            ("package.json", '{"dependencies": {}}', "javascript-review.md"),
            ("Package.swift", "// swift-tools-version:6.0\n", "ios-review.md"),
            ("go.mod", "module example.com/m\n", "go-review.md"),
            # No Rust checklist ships in the kit; Rust gets the cross-language
            # one, which is the honest answer and not a dead map entry.
            ("Cargo.toml", '[package]\nname = "p"\n', "valid-patterns.md"),
        ],
    )
    def test_each_stack_reaches_its_review_checklist(
        self, review_dir, tmp_path, manifest, body, expected,
    ):
        project = tmp_path / "proj"
        project.mkdir()
        (project / manifest).write_text(body)
        keys = reference_docs.detect_stack_keys(project)
        names = reference_docs.doc_names_for(keys)
        assert expected in names, f"{manifest} maps to no {expected}"
        resolved = reference_docs.resolve_docs(names)
        assert expected in [p.name for p, _ in resolved]

    def test_a_stack_whose_docs_are_all_review_docs_still_resolves(self, review_dir, tmp_path):
        """A Go project has no `dev/` doc at all, so it is the case that proves
        the search and the map were widened together."""
        project = tmp_path / "svc"
        project.mkdir()
        (project / "go.mod").write_text("module example.com/svc\n")
        names = reference_docs.doc_names_for(reference_docs.detect_stack_keys(project))
        resolved = reference_docs.resolve_docs(names)
        assert [p.name for p, _ in resolved] == ["go-review.md", "valid-patterns.md"]
        assert all(p.parent.name == "review" for p, _ in resolved)

    def test_the_cross_language_checklist_reaches_every_stack(self):
        for key in reference_docs.STACK_DOCS:
            names = reference_docs.doc_names_for([key])
            # django/react/fastapi are framework keys that only ever appear
            # alongside their language key, which carries the checklist.
            if key in ("django", "react", "fastapi"):
                continue
            assert "valid-patterns.md" in names, key


class TestMappedDocsMissingFromDisk:
    """The map naming a doc nobody wrote is the one case the block is supposed
    to be *loud* about — an INFO line is what makes a missing standard
    non-silent, which is the whole premise of issue #204.

    It is also the newly reachable path: the gate widened from "does
    `reference/dev` exist" to "does `reference/` hold a doc directory", so a
    kit shipping only `review/` now gets this far. Nothing covered it, which is
    how a `NameError` in the log call shipped green.
    """

    def _setup(self, tmp_path, monkeypatch, dev_docs: tuple[str, ...]):
        """cwd is a Node project whose docs are absent; the prompt names a
        Python project whose docs are present."""
        config = tmp_path / "config"
        (config / "reference" / "dev").mkdir(parents=True)
        for name in dev_docs:
            write_doc(config / "reference" / "dev", name, ["Layout"])
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

        node = tmp_path / "web"
        node.mkdir()
        (node / "package.json").write_text('{"dependencies": {}}')
        py = tmp_path / "py"
        py.mkdir()
        (py / "pyproject.toml").write_text('[project]\nname = "py"\n')
        return node, py

    @staticmethod
    def _block(prompt, cwd):
        import context_manager

        return context_manager._dev_reference_block(prompt, str(cwd), {}, 1)

    # Python-only names on purpose: `valid-patterns.md` is mapped to every
    # stack, so putting it on disk would let the Node project resolve too and
    # the test would stop being about a candidate that resolves nothing.
    PY_DOCS = (
        "uv-python-best-practices.md",
        "python-project-structure.md",
        "python-review.md",
    )

    def test_a_candidate_with_no_docs_on_disk_does_not_abandon_the_others(
        self, tmp_path, monkeypatch,
    ):
        """The Node project's doc is not on disk. That must cost the Node
        project its block and nothing else — the Python project the prompt
        named is still a candidate."""
        node, py = self._setup(tmp_path, monkeypatch, self.PY_DOCS)
        block, project = self._block("check ../py/ please", node)
        assert project == py.resolve()
        assert "=== DEV REFERENCES ===" in block
        assert "uv-python-best-practices.md" in block

    def test_the_miss_is_logged_at_info_naming_what_was_searched(
        self, tmp_path, monkeypatch, caplog,
    ):
        """A contentless DEBUG "detection failed" is not a diagnostic. The line
        has to say which docs were wanted and which directories were looked
        in, because "renamed on the kit side" and "kit not installed" are
        different problems with the same symptom."""
        import context_manager

        node, _ = self._setup(tmp_path, monkeypatch, ())
        with caplog.at_level("INFO", logger=context_manager.logger.name):
            block, project = self._block("no path here", node)

        assert (block, project) == ("", None)
        assert "resolved=none" in caplog.text
        assert "bun-vite-react-best-practices.md" in caplog.text
        assert str(tmp_path / "config" / "reference" / "dev") in caplog.text
        assert "Dev reference detection failed" not in caplog.text
