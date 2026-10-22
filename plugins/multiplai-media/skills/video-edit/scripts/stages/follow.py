"""Camera follow for screen demos: push in on each click, pan between them.

A segment's `follow` lists the points to visit ({t, x, y}: source seconds,
position 0..1 in the source frame). The camera starts wide. It moves in over
`lead` seconds so that it arrives on a point at its time, stays there for
`hold` seconds, and then either moves on to the next point or, when the next
one is more than WIDE_AFTER_S away, goes back to the whole frame.

Rendered with ffmpeg `zoompan` (one output frame per input frame): `crop`
cannot animate the zoom, because it fixes its width and height when the
filter starts. Moves ease in and out (smoothstep), like the HyperFrames
`cursor-zoom-follow` block, which this follows in spirit; no code is copied.

Standard library only: the media test suite runs without the workspace.
"""

from __future__ import annotations

from stages.layouts import _num

WIDE_AFTER_S = 2.0  # a longer wait between points goes back to the whole frame


def keys(
    targets: list[tuple[float, float, float]],
    scale: float,
    lead: float,
    hold: float,
    duration: float,
) -> list[tuple[float, float, float, float]]:
    """Camera keyframes (t, zoom, centre x, centre y) for points (t, x, y),
    with every time in seconds from the start of the segment. A point that
    comes while the camera is still holding on the one before is dropped.
    Between keyframes the camera eases from one to the next; it holds
    outside them."""
    wide = (1.0, 0.5, 0.5)
    kept: list[tuple[float, float, float]] = []
    for tg in targets:
        if not kept or tg[0] > kept[-1][0] + hold:
            kept.append(tg)  # a point inside the last hold: the camera stays put
    out = [(0.0, *wide)]
    for i, (t, x, y) in enumerate(kept):
        prev_t, pz, px, py = out[-1]
        start = max(prev_t, t - lead)
        if start > prev_t:
            out.append((start, pz, px, py))  # rest where the camera was until the move
        out.append((t, scale, x, y))
        end = min(t + hold, duration)
        if end > t:
            out.append((end, scale, x, y))
        nxt = kept[i + 1][0] if i + 1 < len(kept) else None
        if nxt is None or nxt - lead - end > WIDE_AFTER_S:
            back = min(end + lead, duration)
            if back > end:
                out.append((back, *wide))
    return out


def ease_expr(points: list[tuple[float, float]], tv: str) -> str:
    """An ffmpeg expression in `tv` through (t, value) points, easing in
    and out between neighbours and holding the first and last values
    outside them. Commas are escaped for use inside a filtergraph."""
    _, v_last = points[-1]
    expr = _num(v_last)
    for (t0, v0), (t1, v1) in reversed(list(zip(points, points[1:]))):
        if t1 <= t0:
            continue
        if v1 == v0:
            seg = _num(v0)
        else:
            u = f"(({tv}-{_num(t0)})/{_num(t1 - t0)})"
            seg = f"{_num(v0)}+({_num(v1 - v0)})*{u}*{u}*(3-2*{u})"
        expr = f"if(lt({tv}\\,{_num(t1)})\\,{seg}\\,{expr})"
    t0, v0 = points[0]
    return f"(if(lt({tv}\\,{_num(t0)})\\,{_num(v0)}\\,{expr}))"


def zoompan_filter(
    cam: list[tuple[float, float, float, float]], fps: int, width: int, height: int
) -> str:
    """`zoompan` driving the camera keyframes on a stream already at `fps`,
    keeping the source size. x and y place the window so the camera centre
    is in the middle of it, clamped so the window stays inside the frame."""
    tv = f"(on/{fps})"
    z = ease_expr([(t, k) for t, k, _, _ in cam], tv)
    cx = ease_expr([(t, x) for t, _, x, _ in cam], tv)
    cy = ease_expr([(t, y) for t, _, _, y in cam], tv)
    x = f"clip({cx}*iw-iw/zoom/2\\,0\\,iw-iw/zoom)"
    y = f"clip({cy}*ih-ih/zoom/2\\,0\\,ih-ih/zoom)"
    return f"zoompan=z={z}:x={x}:y={y}:d=1:s={width}x{height}:fps={fps}"
