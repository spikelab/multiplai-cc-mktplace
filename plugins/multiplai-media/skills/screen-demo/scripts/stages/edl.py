from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Title:
    line1: str
    line2: str = ""
    duration: float = 3.0


@dataclass
class Zoom:
    scale: float = 1.2          # >1 zooms in
    x: float = 0.5              # normalized crop position [0..1] (0.5 = centered)
    y: float = 0.5              # normalized crop position [0..1] (0.5 = centered)
    hold: bool = False          # allow this zoom on the FINAL segment (deliberate close-up ending)


@dataclass
class Segment:
    src_start: float
    src_end: float
    speed: float = 1.0          # >1 = faster, <1 = slower
    zoom: Optional[Zoom] = None
    mute: bool = False          # replace audio with silence (auto-on for speed>4)

    @property
    def src_duration(self) -> float:
        return self.src_end - self.src_start

    @property
    def duration(self) -> float:
        return self.src_duration / self.speed


@dataclass
class Transition:
    after: int
    kind: str = "fade"
    duration: float = 0.5


@dataclass
class Logo:
    path: str
    position: str = "br"           # br | bl | tr | tl
    scale: float = 0.06            # fraction of frame width
    start_at: Optional[float] = None  # default: end of title card


@dataclass
class Music:
    file: Optional[str] = None
    url: Optional[str] = None
    synth: Optional[str] = None    # "calm" | "warm" | "bright" (ffmpeg pink-noise bed)
    prompt: Optional[str] = None   # future: ACE-Step (MPS/CUDA only)
    duration: Optional[float] = None
    volume_db: float = -10.0       # bed attenuation under narration (after loudnorm to -16 LUFS)


@dataclass
class Output:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    crf: int = 18
    audio_bitrate: str = "192k"


@dataclass
class EDL:
    source: str
    segments: list[Segment]
    title: Optional[Title] = None
    transitions: list[Transition] = field(default_factory=list)
    logo: Optional[Logo] = None
    music: Optional[Music] = None
    output: Output = field(default_factory=Output)

    @classmethod
    def load(cls, path: str | Path) -> "EDL":
        data = json.loads(Path(path).read_text())
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, d: dict) -> "EDL":
        def mk_segment(s: dict) -> Segment:
            z = s.pop("zoom", None)
            seg = Segment(**s)
            if z:
                seg.zoom = Zoom(**z)
            return seg

        return cls(
            source=d["source"],
            segments=[mk_segment(dict(s)) for s in d["segments"]],
            title=Title(**d["title"]) if d.get("title") else None,
            transitions=[Transition(**t) for t in d.get("transitions", [])],
            logo=Logo(**d["logo"]) if d.get("logo") else None,
            music=Music(**d["music"]) if d.get("music") else None,
            output=Output(**d.get("output", {})),
        )

    def total_duration(self) -> float:
        title_d = self.title.duration if self.title else 0.0
        seg_d = sum(s.duration for s in self.segments)
        xfade_d = sum(t.duration for t in self.transitions)
        return title_d + seg_d - xfade_d

    def validate(self) -> list[str]:
        """Sanity-check the cut plan before rendering.

        Raises ValueError on plan-breaking mistakes; returns a list of warning
        strings for questionable-but-legal choices.
        """
        warnings: list[str] = []

        src = Path(self.source)
        if src.name.startswith("proxy_") or ".screen-demo-cache" in src.parts or (
            ".cache" in src.parts and "screen-demo" in src.parts
        ):
            raise ValueError(
                f"EDL source points at the analysis proxy ({self.source}). "
                "The proxy is 720p and re-encoding it produces a blurry result — "
                "set `source` to the ORIGINAL recording."
            )

        if self.segments:
            last = self.segments[-1]
            if last.zoom and not last.zoom.hold:
                raise ValueError(
                    "The final segment is zoomed — the video would END cropped, hiding "
                    "part of the screen. End on a full-frame segment (add one after the "
                    "zoom), or set zoom.hold=true if a close-up ending is deliberate."
                )

        for i, s in enumerate(self.segments):
            if s.zoom and s.duration > 12.0:
                warnings.append(
                    f"segment {i} is zoomed for {s.duration:.0f}s — zooms work best as "
                    "short money shots (<12s); viewers lose surrounding context."
                )
        zoomed = sum(s.duration for s in self.segments if s.zoom)
        total = sum(s.duration for s in self.segments)
        if total > 0 and zoomed / total > 0.5:
            warnings.append(
                f"{zoomed/total:.0%} of the runtime is zoomed — most of the demo hides "
                "part of the screen. Prefer full-frame with a few zoomed money shots."
            )
        return warnings
