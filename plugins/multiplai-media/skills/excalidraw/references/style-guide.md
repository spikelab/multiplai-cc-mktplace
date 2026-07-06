# Excalidraw Style Guide — Vintage-Tech Dark Teal

A default palette for Excalidraw's element model. Replace the colors below with
your own brand's if you have a visual style guide (e.g. swap the teal accent and
dark charcoal background for your brand colors).

---

## Color Palette

| Role | Color | Hex | Usage |
|------|-------|-----|-------|
| Canvas background | Dark charcoal | `#222831` | `appState.viewBackgroundColor` |
| Shape fill | Secondary dark | `#393E46` | `backgroundColor` on most elements |
| Stroke / text | Off-white | `#EEEEEE` | `strokeColor`, label text |
| Accent | Teal | `#00ADB5` | Sparingly — databases, key services, critical paths |
| Arrow default | Transparent bg | `transparent` | Arrows have no fill |

### Accent Rules

- **Most elements**: `strokeColor: "#EEEEEE"`, `backgroundColor: "#393E46"`
- **Accent elements** (focal points, databases, key services): `strokeColor: "#00ADB5"`, `backgroundColor: "#393E46"`
- **Accent arrows** (critical paths, primary flows): `strokeColor: "#00ADB5"`
- **Regular arrows**: `strokeColor: "#EEEEEE"`
- Accent teal on no more than ~25% of elements — sparingly, like the brand guide says

---

## Element Defaults

Apply to every element unless overridden:

```json
{
  "roughness": 1,
  "strokeWidth": 2,
  "fillStyle": "solid",
  "opacity": 100,
  "strokeColor": "#EEEEEE",
  "backgroundColor": "#393E46",
  "fontFamily": 3,
  "fontSize": 16,
  "textAlign": "center",
  "verticalAlign": "middle",
  "roundness": { "type": 3 }
}
```

- `roughness: 1` — hand-drawn look, matches retro-tech feel
- `fontFamily: 3` — monospace (Cascadia), matches "monospaced, minimal" brand rule
- `roundness.type: 3` — slightly rounded corners on rectangles

---

## Spacing & Layout

| Property | Value | Notes |
|----------|-------|-------|
| Horizontal gap | 260px | Between adjacent elements in a row |
| Vertical gap | 140px | Between rows |
| Rectangle | 200 x 80 | Standard shape |
| Ellipse | 160 x 80 | Start/end nodes, actors |
| Diamond | 140 x 140 | Decision points |
| Canvas padding | 60px | From edge to first/last element |
| Grid unit | 20px | All coordinates as multiples of 20 |

### Coordinate Calculation

For a 3-column horizontal layout:
- Col 1: x = 60
- Col 2: x = 60 + 200 + 260 = 520
- Col 3: x = 520 + 200 + 260 = 980

For a 3-row vertical layout:
- Row 1: y = 60
- Row 2: y = 60 + 80 + 140 = 280
- Row 3: y = 280 + 80 + 140 = 500

---

## JSON Templates

### Minimal File

```json
{
  "type": "excalidraw",
  "version": 2,
  "source": "claude-code",
  "elements": [],
  "appState": {
    "gridSize": 20,
    "viewBackgroundColor": "#222831"
  },
  "files": {}
}
```

### Rectangle with Label

```json
{
  "type": "rectangle",
  "id": "my-service",
  "x": 60,
  "y": 60,
  "width": 200,
  "height": 80,
  "strokeColor": "#EEEEEE",
  "backgroundColor": "#393E46",
  "fillStyle": "solid",
  "strokeWidth": 2,
  "roughness": 1,
  "opacity": 100,
  "roundness": { "type": 3 },
  "label": {
    "text": "My Service",
    "fontSize": 16,
    "fontFamily": 3,
    "textAlign": "center",
    "verticalAlign": "middle"
  }
}
```

### Accent Rectangle (Database / Key Service)

```json
{
  "type": "rectangle",
  "id": "user-db",
  "x": 60,
  "y": 280,
  "width": 200,
  "height": 80,
  "strokeColor": "#00ADB5",
  "backgroundColor": "#393E46",
  "fillStyle": "solid",
  "strokeWidth": 2,
  "roughness": 1,
  "opacity": 100,
  "roundness": { "type": 3 },
  "label": {
    "text": "User DB",
    "fontSize": 16,
    "fontFamily": 3,
    "textAlign": "center",
    "verticalAlign": "middle"
  }
}
```

### Ellipse

```json
{
  "type": "ellipse",
  "id": "user-actor",
  "x": 60,
  "y": 60,
  "width": 160,
  "height": 80,
  "strokeColor": "#EEEEEE",
  "backgroundColor": "#393E46",
  "fillStyle": "solid",
  "strokeWidth": 2,
  "roughness": 1,
  "opacity": 100,
  "label": {
    "text": "User",
    "fontSize": 16,
    "fontFamily": 3,
    "textAlign": "center",
    "verticalAlign": "middle"
  }
}
```

### Diamond (Decision)

```json
{
  "type": "diamond",
  "id": "auth-check",
  "x": 90,
  "y": 60,
  "width": 140,
  "height": 140,
  "strokeColor": "#EEEEEE",
  "backgroundColor": "#393E46",
  "fillStyle": "solid",
  "strokeWidth": 2,
  "roughness": 1,
  "opacity": 100,
  "label": {
    "text": "Auth?",
    "fontSize": 16,
    "fontFamily": 3,
    "textAlign": "center",
    "verticalAlign": "middle"
  }
}
```

### Arrow (Horizontal, Left to Right)

```json
{
  "type": "arrow",
  "id": "api-to-db",
  "x": 260,
  "y": 100,
  "width": 260,
  "height": 0,
  "points": [[0, 0], [260, 0]],
  "strokeColor": "#EEEEEE",
  "backgroundColor": "transparent",
  "fillStyle": "solid",
  "strokeWidth": 2,
  "roughness": 1,
  "opacity": 100,
  "startArrowhead": null,
  "endArrowhead": "arrow"
}
```

### Arrow (Vertical, Top to Bottom)

```json
{
  "type": "arrow",
  "id": "frontend-to-api",
  "x": 160,
  "y": 140,
  "width": 0,
  "height": 140,
  "points": [[0, 0], [0, 140]],
  "strokeColor": "#EEEEEE",
  "backgroundColor": "transparent",
  "fillStyle": "solid",
  "strokeWidth": 2,
  "roughness": 1,
  "opacity": 100,
  "startArrowhead": null,
  "endArrowhead": "arrow"
}
```

### Accent Arrow (Critical Path)

Same as regular arrow but with `strokeColor: "#00ADB5"`.

### Arrow with Label

```json
{
  "type": "arrow",
  "id": "request-flow",
  "x": 260,
  "y": 100,
  "width": 260,
  "height": 0,
  "points": [[0, 0], [260, 0]],
  "strokeColor": "#EEEEEE",
  "backgroundColor": "transparent",
  "fillStyle": "solid",
  "strokeWidth": 2,
  "roughness": 1,
  "opacity": 100,
  "startArrowhead": null,
  "endArrowhead": "arrow",
  "label": {
    "text": "HTTP/REST",
    "fontSize": 14,
    "fontFamily": 3
  }
}
```

---

## Brand Tone Rules

From the visual style guide — apply to diagram aesthetics:

- **Moody, minimal, precise** — no visual clutter
- **No gradients, no glossy effects** — flat fills only
- **Keep negative space** — don't pack elements tightly
- **Accent teal sparingly** — it's a highlight, not the default
- **Monospaced typography** — all text in fontFamily 3
- **Hand-drawn roughness** — roughness 1 for retro-tech feel, not clinical straight lines
