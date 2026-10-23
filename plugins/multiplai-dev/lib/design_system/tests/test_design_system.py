"""The settings, the token check and the CLI of design_system. No network, no model."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from design_system import (
    NEUTRAL,
    SettingsError,
    check_folder,
    contrast,
    names,
    resolve,
    systems_dir,
)
from design_system.__main__ import main
from design_system.settings import DIR_KEY, NAME_KEY


def _copy(base: Path, name: str) -> Path:
    dest = base / name
    shutil.copytree(NEUTRAL, dest)
    return dest


@pytest.fixture
def systems(tmp_path: Path) -> Path:
    base = tmp_path / "systems"
    base.mkdir()
    _copy(base, "acme")
    _copy(base, "globex")
    return base


@pytest.fixture(autouse=True)
def no_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Neither setting from the environment, and a multiplai.conf location that does not exist."""
    monkeypatch.delenv(DIR_KEY, raising=False)
    monkeypatch.delenv(NAME_KEY, raising=False)
    monkeypatch.setenv("CLAUDE_MULTIPLAI_HOME", str(tmp_path / "no-kit"))


def _conf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    home = tmp_path / "kit"
    home.mkdir(exist_ok=True)
    (home / "multiplai.conf").write_text(text)
    monkeypatch.setenv("CLAUDE_MULTIPLAI_HOME", str(home))


# --- settings ---------------------------------------------------------------


def test_unset_gives_neutral() -> None:
    assert systems_dir() is None
    assert resolve() == NEUTRAL
    assert names() == ["neutral"]


def test_neutral_ships_in_the_plugin() -> None:
    assert NEUTRAL.parts[-4:] == ("skills", "design-system", "assets", "neutral")
    assert (NEUTRAL / "tokens.css").is_file() and (NEUTRAL / "README.md").is_file()


def test_multiplai_conf_is_read(
    systems: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _conf(tmp_path, monkeypatch, f'{DIR_KEY}="{systems}"\n{NAME_KEY}="acme"\n')
    assert resolve() == systems / "acme"


def test_environment_beats_multiplai_conf(
    systems: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _conf(
        tmp_path,
        monkeypatch,
        f'{DIR_KEY}="{tmp_path / "elsewhere"}"\n{NAME_KEY}="acme"\n',
    )
    monkeypatch.setenv(DIR_KEY, str(systems))
    monkeypatch.setenv(NAME_KEY, "globex")
    assert resolve() == systems / "globex"


def test_a_name_argument_beats_the_default(
    systems: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DIR_KEY, str(systems))
    monkeypatch.setenv(NAME_KEY, "globex")
    assert resolve("acme") == systems / "acme"
    assert resolve("neutral") == NEUTRAL


def test_tilde_is_expanded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(DIR_KEY, "~/systems")
    assert systems_dir() == tmp_path / "systems"


def test_a_relative_path_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DIR_KEY, "systems")
    with pytest.raises(SettingsError, match="absolute"):
        systems_dir()


def test_an_unknown_name_errors_and_lists_the_names(
    systems: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DIR_KEY, str(systems))
    with pytest.raises(SettingsError) as e:
        resolve("initech")
    assert "neutral, acme, globex" in str(e.value)


def test_a_name_with_no_folder_set_errors_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NAME_KEY, "acme")
    with pytest.raises(SettingsError, match=DIR_KEY):
        resolve()


# --- check ------------------------------------------------------------------


def test_neutral_passes_check() -> None:
    assert check_folder(NEUTRAL) == []


def _edit(folder: Path, old: str, new: str, count: int = 1) -> None:
    css = folder / "tokens.css"
    text = css.read_text()
    assert old in text
    css.write_text(text.replace(old, new, count))


def test_check_catches_a_token_missing_from_one_dark_block(systems: Path) -> None:
    acme = systems / "acme"
    text = (acme / "tokens.css").read_text()
    # Drop --sunk from the last block only (:root[data-theme="dark"]).
    head, sep, tail = text.rpartition(':root[data-theme="dark"]')
    tail = "\n".join(line for line in tail.splitlines() if "--sunk:" not in line)
    (acme / "tokens.css").write_text(head + sep + tail)
    problems = check_folder(acme)
    assert problems[0] == 'dark (:root[data-theme="dark"]) is missing --sunk'
    # The light --sunk it falls back to is then checked under dark text, and fails.
    assert problems[1:] == [
        'dark (:root[data-theme="dark"]): --fg on --sunk is 1.06:1, below 4.5:1'
    ]


def test_check_catches_a_token_missing_from_the_light_block(systems: Path) -> None:
    acme = systems / "acme"
    _edit(acme, "  --measure: 72ch;\n", "")
    assert check_folder(acme) == ["light (:root) is missing --measure"]


def test_check_catches_a_dark_block_without_color_scheme(systems: Path) -> None:
    acme = systems / "acme"
    _edit(acme, "    color-scheme: dark;\n", "")
    assert any("does not set color-scheme: dark" in p for p in check_folder(acme))


def test_check_catches_a_pair_below_aa(systems: Path) -> None:
    acme = systems / "acme"
    _edit(acme, "--muted: #565e68;", "--muted: #a0a0a0;")
    problems = check_folder(acme)
    assert any(
        p.startswith("light (:root): --muted on --bg is") and "below 4.5:1" in p
        for p in problems
    )


def test_check_resolves_var_references(systems: Path) -> None:
    acme = systems / "acme"
    _edit(acme, "--code-fg: #1b1f24;", "--code-fg: var(--fg);")
    assert check_folder(acme) == []


def test_check_fails_a_colour_it_cannot_read(systems: Path) -> None:
    acme = systems / "acme"
    _edit(acme, "--accent: #1f5fc4;", "--accent: color-mix(in srgb, blue 80%, black);")
    assert any("cannot check --accent on --bg" in p for p in check_folder(acme))


def test_check_catches_a_style_end_tag(systems: Path) -> None:
    acme = systems / "acme"
    (acme / "fonts.css").write_text("/* </style><script>alert(1)</script> */\n")
    assert any(p.startswith("fonts.css contains </style") for p in check_folder(acme))


def test_check_needs_tokens_and_readme(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    assert check_folder(tmp_path / "x") == [
        "README.md is missing",
        "tokens.css is missing",
    ]
    assert check_folder(tmp_path / "nope") == [
        f"{tmp_path / 'nope'} is not a directory"
    ]


def test_contrast_matches_known_values() -> None:
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#777", "#fff") == pytest.approx(4.48, abs=0.01)
    assert contrast("rgb(255, 255, 255)", "#fff") == pytest.approx(1.0)
    assert contrast("#00000080", "#fff") is None


# --- CLI --------------------------------------------------------------------


def test_cli_path_with_no_settings_prints_neutral(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["path"]) == 0
    assert capsys.readouterr().out.strip() == str(NEUTRAL)


def test_cli_check_exit_codes(
    systems: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "neutral"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("check: ok")
    acme = systems / "acme"
    _edit(acme, "  --radius: 6px;\n", "")
    assert main(["check", str(acme)]) == 1
    out = capsys.readouterr().out
    assert "FAIL light (:root) is missing --radius" in out
    assert "--fg on --bg 15.45:1" in out


def test_cli_unknown_name_exits_1_with_the_names(
    systems: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(DIR_KEY, str(systems))
    assert main(["path", "initech"]) == 1
    assert "available: neutral, acme, globex" in capsys.readouterr().err


def test_cli_list_marks_the_default(
    systems: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(DIR_KEY, str(systems))
    monkeypatch.setenv(NAME_KEY, "acme")
    assert main(["list"]) == 0
    assert capsys.readouterr().out.splitlines()[1:] == [
        "  neutral",
        "* acme",
        "  globex",
    ]


def test_cli_show_prints_readme_and_tokens(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["show"]) == 0
    out = capsys.readouterr().out
    assert (
        "# neutral" in out and "--accent: #1f5fc4" in out and "--accent: #7eaaf0" in out
    )


def test_cli_create_copies_neutral_and_refuses_an_existing_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = tmp_path / "made"
    assert main(["create", "acme", "--dir", str(base)]) == 0
    assert check_folder(base / "acme") == []
    (base / "acme" / "tokens.css").write_text("edited")
    assert main(["create", "acme", "--dir", str(base)]) == 1  # S1: never overwrites
    assert "already exists" in capsys.readouterr().err
    assert (base / "acme" / "tokens.css").read_text() == "edited"


def test_cli_create_needs_a_folder(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["create", "acme"]) == 1
    assert DIR_KEY in capsys.readouterr().err
    assert main(["create", "neutral", "--dir", "/tmp/x"]) == 1
    assert main(["create", "acme", "--dir", "relative"]) == 1
