from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


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
class Focus:
    x: float = 0.5              # normalized [0..1] point to keep in view when cropping
    y: float = 0.5


@dataclass
class Segment:
    src_start: float
    src_end: float
    speed: float = 1.0          # >1 = faster, <1 = slower
    zoom: Optional[Zoom] = None
    mute: bool = False          # replace audio with silence (auto-on for speed>4)
    focus: Optional[Focus] = None   # crop centre for fit "crop" (in the frame) or a panel frame (in the panel)
    frame: Optional[str] = None     # "stack" | a panel name ("A", "B") | "speaker"
    fit: Optional[str] = None       # "pad" | "blur" | "crop": overrides output.fit for this segment

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
    fit: str = "pad"            # "pad" | "blur" | "crop" — how a segment without a frame fills the output
    audio_rate: Optional[int] = None    # resample the final audio (reels: 48000)


FITS = ("pad", "blur", "crop")


@dataclass
class Panel:
    x: int
    y: int
    w: int
    h: int


@dataclass
class Layout:
    panels: dict[str, Panel] = field(default_factory=dict)   # name -> rectangle, source pixels
    speakers: dict[str, str] = field(default_factory=dict)   # transcript speaker label -> panel name


@dataclass
class Captions:
    words_per_line: int = 5     # a soft cap; max_chars is the hard limit (stages/captions.py)
    max_chars: int = 22
    position_y: float = 0.68    # vertical centre of the caption line, fraction of output height
    highlight: bool = True      # colour the word being spoken
    size: Optional[int] = None  # font size in output pixels; default scales with output height


@dataclass
class Region:
    x: int                      # output pixels
    y: int
    w: int
    h: int


OVERLAY_MODES = ("panel", "keyed")


@dataclass
class Overlay:
    """A motion graphic (an HTML page, stages/overlay.py) shown from start to end.

    `panel` covers its region with the page as rendered; `keyed` removes the
    page's `key_color` background first (colorkey + despill), so the video
    shows around the graphic. Without a region it covers the whole frame.
    """
    html: str
    start: float                # output time
    end: float
    mode: str = "keyed"
    region: Optional[Region] = None
    key_color: str = "#00FF00"
    style: Optional[str] = None  # the style file the page follows (references/styles/)

    def rect(self, width: int, height: int) -> Region:
        return self.region or Region(0, 0, width, height)


@dataclass
class Headline:
    text: str
    start: float = 0.0          # output time
    end: float = 3.0


@dataclass
class EDL:
    source: str
    segments: list[Segment]
    title: Optional[Title] = None
    transitions: list[Transition] = field(default_factory=list)
    logo: Optional[Logo] = None
    music: Optional[Music] = None
    output: Output = field(default_factory=Output)
    transcript: Optional[str] = None    # transcript.json; default: the prep cache for `source`
    layout: Optional[Layout] = None
    captions: Optional[Captions] = None
    headline: Optional[Headline] = None
    brand: Optional[str] = None         # path to a brand.json (stages/brand.py)
    overlays: list[Overlay] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "EDL":
        data = json.loads(Path(path).read_text())
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, d: dict) -> "EDL":
        def mk_segment(s: dict) -> Segment:
            z = s.pop("zoom", None)
            f = s.pop("focus", None)
            seg = Segment(**s)
            if z:
                seg.zoom = Zoom(**z)
            if f:
                seg.focus = Focus(**f)
            return seg

        def mk_overlay(o: dict) -> Overlay:
            r = o.pop("region", None)
            return Overlay(**o, region=Region(**r) if r else None)

        def mk_layout(d: dict) -> Layout:
            return Layout(panels={k: Panel(**v) for k, v in d.get("panels", {}).items()},
                          speakers=dict(d.get("speakers", {})))

        return cls(
            source=d["source"],
            segments=[mk_segment(dict(s)) for s in d["segments"]],
            title=Title(**d["title"]) if d.get("title") else None,
            transitions=[Transition(**t) for t in d.get("transitions", [])],
            logo=Logo(**d["logo"]) if d.get("logo") else None,
            music=Music(**d["music"]) if d.get("music") else None,
            output=Output(**d.get("output", {})),
            transcript=d.get("transcript"),
            layout=mk_layout(d["layout"]) if d.get("layout") else None,
            captions=Captions(**d["captions"]) if d.get("captions") is not None else None,
            headline=Headline(**d["headline"]) if d.get("headline") else None,
            brand=d.get("brand"),
            overlays=[mk_overlay(dict(o)) for o in d.get("overlays", [])],
        )

    def xfade_before(self, segment_index: int) -> float:
        """Crossfade into segment `segment_index`: the transition declared
        `after` the previous one (-1 for the title card); else 0 when the
        segment starts where the previous one ended in the source (one
        continuous take, so a hard cut with unbroken audio); else 0.5 s.
        A duration of 0 is a hard cut."""
        for t in self.transitions:
            if t.after == segment_index - 1:
                return t.duration
        if 0 < segment_index < len(self.segments) and \
                self.segments[segment_index].src_start == self.segments[segment_index - 1].src_end:
            return 0.0
        return 0.5

    def total_duration(self) -> float:
        """Rendered length: the title card, then each segment overlapping the
        one before by its crossfade (the same arithmetic as
        composite.build_filter_complex and timeline.place_segments)."""
        end = self.title.duration if self.title else 0.0
        for i, seg in enumerate(self.segments):
            start = 0.0 if i == 0 and not self.title else end - self.xfade_before(i)
            end = start + seg.duration
        return end

    def validate(self, source_size: Optional[tuple[int, int]] = None,
                 words: Optional[list[dict]] = None) -> list[str]:
        """Sanity-check the cut plan before rendering.

        Raises ValueError on plan-breaking mistakes; returns a list of warning
        strings for questionable-but-legal choices. `source_size` (w, h) enables
        the framing checks that need it; `words` (the transcript contract's
        words) enables the `frame: "speaker"` check.
        """
        warnings: list[str] = []
        warnings += self._validate_framing(source_size, words)
        self._validate_overlays()
        warnings += [f"overlay {i} ({Path(o.html).name}) names no style; ask the user which style to follow"
                     for i, o in enumerate(self.overlays) if not o.style]

        src = Path(self.source)
        if src.name.startswith("proxy_") or ".video-edit-cache" in src.parts or (
            ".cache" in src.parts and "video-edit" in src.parts
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

    def output_duration(self) -> float:
        from stages.timeline import place_segments   # timeline imports this module
        placed = place_segments(self)
        return placed[-1].out_end if placed else (self.title.duration if self.title else 0.0)

    def _validate_overlays(self) -> None:
        if not self.overlays:
            return
        W, H = self.output.width, self.output.height
        total = self.output_duration()
        for i, o in enumerate(self.overlays):
            name = f"overlay {i} ({Path(o.html).name})"
            if o.mode not in OVERLAY_MODES:
                raise ValueError(f"{name}: mode {o.mode!r} is not one of {', '.join(OVERLAY_MODES)}.")
            if not (0 <= o.start < o.end <= total + 1e-3):
                raise ValueError(f"{name}: {o.start}–{o.end}s must lie within the video (0–{total:.2f}s) "
                                 "and end after it starts.")
            r = o.rect(W, H)
            if r.w <= 0 or r.h <= 0 or r.x < 0 or r.y < 0 or r.x + r.w > W or r.y + r.h > H:
                raise ValueError(f"{name}: region {r.x},{r.y} {r.w}x{r.h} is not inside the {W}x{H} frame.")
            if r.w % 2 or r.h % 2:
                raise ValueError(f"{name}: region width and height must be even, got {r.w}x{r.h}.")
            if o.mode == "keyed" and not _HEX.match(o.key_color):
                raise ValueError(f"{name}: key_color {o.key_color!r} is not #RRGGBB.")
        for i, a in enumerate(self.overlays):
            for j in range(i + 1, len(self.overlays)):
                b = self.overlays[j]
                ra, rb = a.rect(W, H), b.rect(W, H)
                same_time = a.start < b.end and b.start < a.end
                same_place = ra.x < rb.x + rb.w and rb.x < ra.x + ra.w and ra.y < rb.y + rb.h and rb.y < ra.y + ra.h
                if same_time and same_place:
                    raise ValueError(f"overlays {i} and {j} cover the same part of the frame at the same "
                                     f"time ({max(a.start, b.start):.2f}–{min(a.end, b.end):.2f}s); "
                                     "move one, shorten one, or give them separate regions.")

    def _validate_framing(self, source_size: Optional[tuple[int, int]],
                          words: Optional[list[dict]]) -> list[str]:
        warnings: list[str] = []
        if self.output.fit not in FITS:
            raise ValueError(f"output.fit {self.output.fit!r} is not one of {', '.join(FITS)}.")
        for i, s in enumerate(self.segments):
            if s.fit is None:
                continue
            if s.fit not in FITS:
                raise ValueError(f"segment {i} fit {s.fit!r} is not one of {', '.join(FITS)}.")
            if s.frame is not None:
                raise ValueError(f"segment {i} sets both fit {s.fit!r} and frame {s.frame!r}; fit "
                                 "fills the output with the whole frame, so drop one of them.")
        for t in self.transitions:
            if t.duration < 0:
                raise ValueError(f"transition after {t.after} has a negative duration ({t.duration}).")
        panels = self.layout.panels if self.layout else {}
        if source_size:
            sw, sh = source_size
            for name, p in panels.items():
                if p.w <= 0 or p.h <= 0 or p.x < 0 or p.y < 0 or p.x + p.w > sw or p.y + p.h > sh:
                    raise ValueError(f"layout panel {name} ({p.x},{p.y} {p.w}x{p.h}) is not inside "
                                     f"the {sw}x{sh} source frame.")
        uses_speaker = False
        for i, s in enumerate(self.segments):
            if s.frame is None:
                continue
            if s.zoom:
                raise ValueError(
                    f"segment {i} has both zoom and frame {s.frame!r}. Panel rectangles are in "
                    "source pixels and the zoom crops the frame before them, so the panel would "
                    "point at the wrong region. Drop the zoom; use focus to choose what the "
                    "panel keeps in view.")
            if s.frame == "stack":
                missing = [k for k in ("A", "B") if k not in panels]
                if missing:
                    raise ValueError(f'segment {i} uses frame "stack", which needs panels A and B '
                                     f"in layout.panels; missing: {', '.join(missing)}.")
            elif s.frame == "speaker":
                uses_speaker = True
            elif s.frame not in panels:
                raise ValueError(f"segment {i} uses frame {s.frame!r}, which is not a panel in "
                                 f"layout.panels ({', '.join(panels) or 'none declared'}).")
        if uses_speaker:
            labelled = bool(words) and all("speaker" in w for w in words or [])
            if not labelled:
                raise ValueError(
                    'frame "speaker" needs a transcript with speaker labels, and this one has '
                    "none. Labels come from the transcribe skill once it can tell speakers "
                    "apart (diarization); that is planned, not built. Until then set frame "
                    'to a panel name or "stack" on each segment.')
            mapping = self.layout.speakers if self.layout else {}
            unknown = sorted({w["speaker"] for w in words or []} - set(mapping))
            if not mapping or unknown:
                raise ValueError('frame "speaker" needs layout.speakers to map every transcript '
                                 f"speaker to a panel; unmapped: {', '.join(unknown) or 'all'}.")
            bad = sorted(v for v in mapping.values() if v not in panels)
            if bad:
                raise ValueError(f"layout.speakers names panels that do not exist: {', '.join(bad)}.")
        if source_size:
            sw, sh = source_size
            portrait_out = self.output.height > self.output.width
            if portrait_out and sw > sh:
                padded = [i for i, s in enumerate(self.segments)
                          if s.frame is None and (s.fit or self.output.fit) == "pad"]
                if padded:
                    warnings.append(
                        f"segments {padded} letterbox a landscape source into a portrait output "
                        '(fit "pad" leaves bars above and below). Use fit "blur" or "crop" '
                        "(in output or on the segment), or give those segments a panel frame.")
        return warnings
