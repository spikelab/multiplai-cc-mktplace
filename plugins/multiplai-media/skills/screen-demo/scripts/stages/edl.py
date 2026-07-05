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
    x: float = 0.5              # normalized horizontal center [0..1]
    y: float = 0.5              # normalized vertical center [0..1]


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
    width: int = 1280
    height: int = 720
    fps: int = 30
    crf: int = 22
    audio_bitrate: str = "128k"


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
