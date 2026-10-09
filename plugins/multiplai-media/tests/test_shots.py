"""Shot list (stages/shots.py): scenes from prep's scenes.csv inside a time
range, and the three frames drawn for each. subprocess.run is replaced."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import shots  # noqa: E402

# The first rows of the Rails World 2026 keynote's scenes.csv, as PySceneDetect writes it.
CSV = """Scene Number,Start Frame,Start Timecode,Start Time (seconds),End Frame,End Timecode,End Time (seconds),Length (frames),Length (timecode),Length (seconds)
1,1,00:00:00.000,0.000,188,00:00:06.267,6.267,188,00:00:06.267,6.267
2,189,00:00:06.267,6.267,407,00:00:13.567,13.567,219,00:00:07.300,7.300
3,408,00:00:13.567,13.567,4019,00:02:13.967,133.967,3612,00:02:00.400,120.400
"""


@pytest.fixture
def scenes_csv(tmp_path: Path) -> Path:
    p = tmp_path / "scenes.csv"
    p.write_text(CSV)
    return p


def test_read_scenes_takes_start_and_end_seconds(scenes_csv: Path) -> None:
    assert shots.read_scenes(scenes_csv) == [(0.0, 6.267), (6.267, 13.567), (13.567, 133.967)]


def test_a_scene_list_with_a_timecode_line_above_the_header_is_read(tmp_path: Path) -> None:
    # Older PySceneDetect versions write the cut timecodes on a first line.
    p = tmp_path / "scenes.csv"
    p.write_text("Timecode List:,00:00:06.267,00:00:13.567\n" + CSV)
    assert len(shots.read_scenes(p)) == 3


def test_a_file_that_is_not_a_scene_list_is_refused(tmp_path: Path) -> None:
    p = tmp_path / "scenes.csv"
    p.write_text("a,b\n1,2\n")
    with pytest.raises(ValueError, match="not a PySceneDetect scene list"):
        shots.read_scenes(p)


def test_shots_are_clipped_to_the_range(scenes_csv: Path) -> None:
    scenes = shots.read_scenes(scenes_csv)
    assert shots.shots_in(scenes, 5.0, 20.0) == [(5.0, 6.267), (6.267, 13.567), (13.567, 20.0)]
    assert shots.shots_in(scenes, 6.267, 13.567) == [(6.267, 13.567)]
    assert shots.shots_in(scenes, 200, 300) == []


def test_three_frames_inside_each_shot() -> None:
    assert shots.frame_times((10.0, 20.0)) == [10.2, 15.0, 19.8]
    # A shot under 0.8 s keeps its frames a quarter of its length from the edges.
    assert shots.frame_times((10.0, 10.4)) == [10.1, 10.2, 10.3]


def test_make_draws_three_tiles_per_shot_then_one_sheet(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(shots.subprocess, "run", lambda cmd, *a, **k: calls.append([str(c) for c in cmd]))
    out = tmp_path / "shots.png"
    shots.make(Path("/c/proxy_720p.mp4"), [(0.0, 6.0), (6.0, 13.0)], out, "/fonts/Bold.ttf")
    assert len(calls) == 7
    assert [c[c.index("-ss") + 1] for c in calls[:6]] == ["0.200", "3.000", "5.800", "6.200", "9.500", "12.800"]
    assert "text='shot 2  9.50s'" in calls[4][calls[4].index("-vf") + 1]
    assert calls[-1][-1] == str(out)
    assert "tile=3x2" in calls[-1][calls[-1].index("-vf") + 1]
