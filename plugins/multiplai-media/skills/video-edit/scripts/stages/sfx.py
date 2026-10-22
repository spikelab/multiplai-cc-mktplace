"""Short sound effects on clicks and graphic entrances.

No sounds ship with the skill: an EDL names each sound by the path to an
audio file the user supplies. composite.render() mixes them in after the
music, so they never duck it.

suggest() proposes a list for an EDL: a click at each follow target and a pop
at each overlay's start, in output time, without two sounds closer than
SFX_MIN_GAP_S. The session pastes it into the EDL; nothing here writes one.
"""

from __future__ import annotations

from pathlib import Path

from stages import timeline
from stages.edl import EDL, SFX_MIN_GAP_S


def resolve(sound: str) -> Path:
    """The audio file a sound names: a path to the user's own file."""
    p = Path(sound).expanduser()
    if p.is_file():
        return p.resolve()
    raise FileNotFoundError(
        f"sound file not found: {p} (a sound is a path to an audio file)"
    )


def suggest(edl: EDL, clicks: str, overlays: str) -> tuple[list[dict], list[str]]:
    """Sounds for each follow target and each overlay's start, in output time.

    `clicks` and `overlays` are the audio files to use for each.

    A target in a cut or a muted (sped-up past 4x) part has no output time
    and is skipped. A sound within SFX_MIN_GAP_S of the one kept before it
    is dropped. Returns the kept list and one line per sound left out.
    """
    placed = timeline.place_segments(edl)
    found: list[tuple[float, str, str]] = []
    notes: list[str] = []
    for i, seg in enumerate(edl.segments):
        for k in seg.follow.targets if seg.follow else []:
            at = timeline.to_output(k.t, placed)
            if at is None:
                notes.append(
                    f"skipped {clicks} for segment {i} target at source {k.t}s: "
                    "that moment is cut or muted in the output"
                )
                continue
            found.append((at, clicks, f"segment {i} target at source {k.t}s"))
    for i, o in enumerate(edl.overlays):
        found.append((o.start, overlays, f"overlay {i} ({Path(o.html).name})"))
    found.sort(key=lambda f: f[0])

    kept: list[dict] = []
    for at, sound, what in found:
        if kept and at - kept[-1]["at"] < SFX_MIN_GAP_S:
            notes.append(
                f"dropped {sound} at {at:.3f}s ({what}): within {SFX_MIN_GAP_S}s of "
                f"the {kept[-1]['sound']} at {kept[-1]['at']:.3f}s"
            )
            continue
        kept.append({"at": round(at, 3), "sound": sound})
    return kept, notes
