"""Where the pointer clicks in a screen recording, for the camera to follow.

Three steps, all on prep's 720p proxy:

1. Motion. Between two frames sampled at FPS, the pointer is the small
   region that changed while the rest of the screen stayed still; its
   position holds while it rests. A click is a burst of change near a
   pointer that has stopped: it rests, then within CLICK_WINDOW_S the
   screen changes near it (a button pressed, a menu opened, a page loaded).
2. Pictures. Moments where the pointer was the only thing that moved give
   pictures of what it looks like in this recording (`harvest`). The user
   can add more by marking the pointer in a still (`marked`): the hand
   pointer seldom moves alone, so it is seldom learned.
3. Refinement. Each click's position is searched for those pictures near
   where motion put it. A match marks the click `sure`; otherwise the
   motion position stays and `sure` is false.

Measured on one 238 s recording (20 frames checked by eye): motion alone put
the pointer within 20 px in 11 of 20 frames; the learned arrow found it in 9
of 9 arrow frames; arrow plus one marked hand, 15 of 19. Clicks themselves
were not checked, so a person reviews the render.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

FPS = 10
DIFF_THRESHOLD = 24  # grey levels: smaller changes are encoder noise
MAX_BLOB_PX = 48  # a pointer blob fits in this square at 720p
MIN_BLOB_AREA = 12  # pixels: smaller is noise
PAGE_CHANGE_FRACTION = 0.02  # more of the frame than this changed: not just the pointer
REST_S = 0.3  # the pointer must rest this long before a click
CLICK_WINDOW_S = 0.6  # the change must come this soon after it stops
CLICK_RADIUS_PX = 120  # and touch a region this close to it


@dataclass
class Sample:
    t: float
    x: float | None
    y: float | None
    moved: bool  # the pointer was seen moving into this position


@dataclass
class Click:
    t: float
    x: float
    y: float


def _frames(path: Path, fps: int):
    import cv2

    cap = cv2.VideoCapture(str(path))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, round(src_fps / fps))
    i = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if i % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            yield i / src_fps, cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        i += 1
    cap.release()


def _blobs(prev, cur):
    """Changed regions between two grey frames: (boxes, changed fraction)."""
    import cv2

    diff = cv2.absdiff(prev, cur)
    _, mask = cv2.threshold(diff, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
    mask = cv2.dilate(mask, None, iterations=2)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask)
    boxes = [
        (
            int(stats[k, 0]),
            int(stats[k, 1]),
            int(stats[k, 2]),
            int(stats[k, 3]),
            int(stats[k, 4]),
            float(cents[k, 0]),
            float(cents[k, 1]),
        )
        for k in range(1, n)
    ]
    frac = float((mask > 0).mean())
    return boxes, frac


def track(path: Path, fps: int = FPS) -> tuple[list[Sample], list[Click]]:
    samples: list[Sample] = []
    clicks: list[Click] = []
    prev = None
    pos: tuple[float, float] | None = None
    rest_since: float | None = None
    clicked_at_rest = False
    for t, frame in _frames(path, fps):
        if prev is None:
            prev = frame
            samples.append(Sample(t, None, None, False))
            continue
        boxes, frac = _blobs(prev, frame)
        small = [
            b
            for b in boxes
            if b[2] <= MAX_BLOB_PX and b[3] <= MAX_BLOB_PX and b[4] >= MIN_BLOB_AREA
        ]
        big = [b for b in boxes if b not in small and b[4] >= MIN_BLOB_AREA]
        moved = False
        if frac < PAGE_CHANGE_FRACTION and small and len(small) <= 3:
            # The pointer leaves one blob where it was and makes one where it is:
            # the new position is the blob farthest from the last known one.
            if pos is None:
                best = max(small, key=lambda b: b[4])
            else:
                best = max(
                    small, key=lambda b: (b[5] - pos[0]) ** 2 + (b[6] - pos[1]) ** 2
                )
            new = (best[5], best[6])
            if pos is None or (new[0] - pos[0]) ** 2 + (new[1] - pos[1]) ** 2 > 4:
                pos, moved = new, True
        if moved:
            rest_since, clicked_at_rest = None, False
        elif pos is not None and rest_since is None:
            rest_since = t
        if (
            pos is not None
            and rest_since is not None
            and not clicked_at_rest
            and REST_S <= t - rest_since + 1 / fps <= REST_S + CLICK_WINDOW_S
        ):
            near = [
                b
                for b in big + small
                if abs(b[5] - pos[0]) < CLICK_RADIUS_PX
                and abs(b[6] - pos[1]) < CLICK_RADIUS_PX
                and b[4] >= 4 * MIN_BLOB_AREA
            ]
            if near or frac >= PAGE_CHANGE_FRACTION:
                clicks.append(Click(round(t, 3), round(pos[0], 1), round(pos[1], 1)))
                clicked_at_rest = True
        samples.append(
            Sample(
                round(t, 3),
                None if pos is None else round(pos[0], 1),
                None if pos is None else round(pos[1], 1),
                moved,
            )
        )
        prev = frame
    return samples, clicks


# --- pictures of the pointer, learned from the recording or marked by the user ---

CERTAIN_MAX_FRACTION = 0.002  # only the pointer moved: almost nothing else changed
PATCH = 24  # reference picture size, proxy pixels
SAME_SHAPE = 0.6  # outline overlap at which two crops show the same pointer
MIN_GROUP = 5  # a look seen in fewer separate places is not kept
MAX_LOOKS = 4
SEARCH_PX = 60  # refine a click within this distance of the motion position
MATCH_MAX_ERR = (
    2500.0  # mean squared grey error over the outline; worse keeps the motion position
)


def _crop(frame, x: float, y: float):
    h = PATCH // 2
    xi, yi = int(round(x)), int(round(y))
    if xi - h < 0 or yi - h < 0 or xi + h > frame.shape[1] or yi + h > frame.shape[0]:
        return None
    return frame[yi - h : yi + h, xi - h : xi + h]


def _outline(crop):
    """The pointer in a crop taken where it was the only thing that moved:
    the dark shape at the centre plus a 2 px ring around it (the
    pointer's light border). None when the crop does not show one dark
    shape on a light background."""
    import cv2
    import numpy as np

    # darker than the light background by a clear margin; at 720p a thin
    # outline (the hand) breaks into specks, so join every speck near the centre
    dark = (crop < float(np.median(crop)) - 50).astype("uint8")
    n, labels, _, cents = cv2.connectedComponentsWithStats(dark)
    near = [
        i
        for i in range(1, n)
        if (cents[i, 0] - PATCH / 2) ** 2 + (cents[i, 1] - PATCH / 2) ** 2 <= 81
    ]
    if not near:
        return None
    fill = np.isin(labels, near).astype("uint8")
    ys, xs = np.nonzero(fill)
    if not 15 <= len(xs) <= 200 or max(np.ptp(xs), np.ptp(ys)) > PATCH - 6:
        return None
    mask = cv2.dilate(fill, np.ones((5, 5), "uint8"))
    ring = mask.astype(bool) & ~fill.astype(bool)
    if crop[ring].mean() < 170:  # the border must be light, or this is text on a page
        return None
    return mask


def harvest(path: Path, fps: int = FPS) -> list:
    """Reference pictures of the pointer, as (picture, outline) pairs, cropped
    where it was the only thing that moved: two pointer-sized changes (where
    it was, where it is) and almost nothing else. Crops whose outlines
    overlap are grouped; each group seen in at least MIN_GROUP separate
    places gives one picture (the crop that overlaps most others)."""
    import numpy as np

    found = []
    prev = None
    last: tuple[float, float] | None = None
    for _, frame in _frames(path, fps):
        if prev is not None:
            boxes, frac = _blobs(prev, frame)
            small = [
                b
                for b in boxes
                if b[2] <= MAX_BLOB_PX and b[3] <= MAX_BLOB_PX and b[4] >= MIN_BLOB_AREA
            ]
            if (
                frac < CERTAIN_MAX_FRACTION
                and len(small) == 2
                and len(boxes) == 2
                and last is not None
            ):
                new = max(
                    small, key=lambda b: (b[5] - last[0]) ** 2 + (b[6] - last[1]) ** 2
                )
                c = _crop(frame, new[5], new[6])
                mask = None if c is None else _outline(c)
                if mask is not None:
                    found.append(
                        (c.copy(), mask, (round(new[5] / 40), round(new[6] / 40)))
                    )
            if small and frac < PAGE_CHANGE_FRACTION:
                last = (
                    (small[-1][5], small[-1][6])
                    if last is None
                    else max(
                        ((b[5], b[6]) for b in small),
                        key=lambda p: (p[0] - last[0]) ** 2 + (p[1] - last[1]) ** 2,
                    )
                )
        prev = frame
    if not found:
        return []
    masks = np.stack([m.reshape(-1) for _, m, _ in found]).astype("float32")
    inter = masks @ masks.T
    area = masks.sum(1)
    iou = inter / (area[:, None] + area[None, :] - inter + 1e-6)
    left = set(range(len(found)))
    looks = []
    while left and len(looks) < MAX_LOOKS:
        idx = sorted(left)
        # count the separate places a shape was seen: a pointer moves about
        # the screen, an icon that animates in place does not
        counts = {
            i: len({found[j][2] for j in idx if iou[i, j] >= SAME_SHAPE}) for i in idx
        }
        best = max(idx, key=counts.get)
        if counts[best] < MIN_GROUP:
            break
        looks.append(found[best][:2])
        left -= {j for j in idx if iou[best, j] >= SAME_SHAPE}
    return looks


def _masked_error(frame, look, mask):
    """Mean squared grey difference between the picture and every position
    of the frame, over the outline pixels only. Expands the square so that
    each term is a plain correlation, which OpenCV computes fast."""
    import cv2

    f = frame.astype("float32")
    m = mask.astype("float32")
    t = look.astype("float32") * m
    sq = cv2.matchTemplate(f * f, m, cv2.TM_CCORR)
    cross = cv2.matchTemplate(f, t, cv2.TM_CCORR)
    return (sq - 2 * cross + float((t * t).sum())) / float(m.sum())


def marked(path: Path, marks: list[tuple[float, float, float]]) -> list:
    """Pictures of the pointer where the user marked it: (t seconds, x, y in
    0..1). A click lands near the pointer, not on its centre, so the crop is
    centred on the dark shape nearest the click first."""
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(path))
    looks = []
    for t, x, y in marks:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        px, py = x * g.shape[1], y * g.shape[0]
        around = _crop(g, px, py)
        if around is None:
            continue
        ys, xs = np.nonzero(around < float(np.median(around)) - 50)
        if len(xs):
            px += float(xs.mean()) - PATCH / 2
            py += float(ys.mean()) - PATCH / 2
        c = _crop(g, px, py)
        mask = None if c is None else _outline(c)
        if mask is not None:
            looks.append((c.copy(), mask))
    cap.release()
    return looks


def refine(path: Path, clicks: list[Click], looks: list) -> list[tuple[Click, bool]]:
    """Each click, moved onto the best picture match within SEARCH_PX of it
    when one scores under MATCH_MAX_ERR; with whether it matched."""
    import cv2

    cap = cv2.VideoCapture(str(path))
    out = []
    for c in clicks:
        cap.set(cv2.CAP_PROP_POS_MSEC, c.t * 1000)
        ok, frame = cap.read()
        best = (MATCH_MAX_ERR, None)
        if ok and looks:
            g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            x0, y0 = max(0, int(c.x) - SEARCH_PX), max(0, int(c.y) - SEARCH_PX)
            win = g[y0 : int(c.y) + SEARCH_PX, x0 : int(c.x) + SEARCH_PX]
            for look, mask in looks:
                if win.shape[0] < PATCH or win.shape[1] < PATCH:
                    break
                err, _, loc, _ = cv2.minMaxLoc(_masked_error(win, look, mask))
                if err < best[0]:
                    best = (err, (x0 + loc[0] + PATCH / 2, y0 + loc[1] + PATCH / 2))
        if best[1] is None:
            out.append((c, False))
        else:
            out.append((Click(c.t, round(best[1][0], 1), round(best[1][1], 1)), True))
    cap.release()
    return out


def find(proxy: Path, marks: list[tuple[float, float, float]] = ()) -> dict:
    """Clicks in the proxy as {t, x, y, sure}, x and y in 0..1."""
    import cv2

    cap = cv2.VideoCapture(str(proxy))
    w, h = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    cap.release()
    if not w or not h:
        raise ValueError(f"cannot read video frames from {proxy}")
    _, clicks = track(proxy)
    looks = harvest(proxy) + marked(proxy, list(marks))
    rows = [
        {"t": c.t, "x": round(c.x / w, 4), "y": round(c.y / h, 4), "sure": sure}
        for c, sure in refine(proxy, clicks, looks)
    ]
    return {"pictures": len(looks), "clicks": rows}
