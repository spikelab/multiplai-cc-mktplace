---
name: excalidraw
description: >
  Generate and iteratively refine Excalidraw diagrams for architecture,
  design exploration, and visual communication. Creates .excalidraw files
  that open in VS Code Excalidraw extension for drawing on top of.
  Triggers on "draw a diagram", "create an excalidraw", "diagram this",
  "visualize this architecture", or explicit /excalidraw invocation.
model: opus
effort: low
---

# Excalidraw Diagram Generator

Generate `.excalidraw` JSON files that open in the VS Code Excalidraw extension. Designed for creating diagrams the user can draw on top of during screencasts.

---

## Arguments

| Arg | Description | Default |
|-----|-------------|---------|
| **description** | What to diagram (rest of invocation text) | *(required)* |
| `--type` | `architecture`, `flow`, `sequence`, `mindmap`, `er`, `custom` | Auto-detect |
| `--output` | File path for output | `INBOX/{slug}.excalidraw` |
| `--layout` | `vertical`, `horizontal`, `hub-spoke` | Auto-detect from type |

---

## Setup

On first invocation, load `references/style-guide.md` for color palette, spacing rules, element defaults, and JSON templates. The style guide derives from `MEMORY/visual-style-guide.md` (Vintage-Tech Dark Teal brand).

---

## Workflow

### 1. UNDERSTAND

- Parse the description to determine what to diagram
- Auto-detect type if not specified:
  - Components + connections → `architecture`
  - Steps + decisions → `flow`
  - Actor interactions over time → `sequence`
  - Hierarchical concepts → `mindmap`
  - Entities + relationships → `er`
- Choose layout: vertical (default for flow/sequence), horizontal (pipeline), hub-spoke (architecture with central service)

### 2. GENERATE

- Build the `.excalidraw` JSON following style-guide.md rules
- Use descriptive IDs (`api-gateway`, `user-db`) — NOT UUIDs
- Use `label` property for text in shapes — do NOT create separate text elements
- Grid-based layout: all coordinates as multiples of 20px
- Cap at **20 elements**. If more are needed, suggest splitting into multiple diagrams.
- Write to the output path (default: `INBOX/{slug}.excalidraw`)

### 3. PRESENT

- Tell the user the file path
- Suggest: "Open it in VS Code — the Excalidraw extension will render it as an interactive canvas."
- Briefly describe the layout and key elements

### 4. WAIT

Ask: **"How does it look? Any changes?"**

### 5. MODIFY (on feedback)

- **Always read the current file before modifying** — the user may have drawn on it in Excalidraw
- Preserve ALL existing elements unless explicitly asked to remove them
- Add/modify only what was requested
- Do NOT regenerate from scratch unless the user asks for a complete redo
- Write updated file

### 6. LOOP

Back to step 4. Continue until the user says done or moves on.

---

## JSON Generation Rules

### File Structure

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

### Element Defaults

Apply these to every element (overrides in style-guide.md):

- `roughness: 1` — hand-drawn look (retro-tech feel)
- `strokeWidth: 2`
- `fillStyle: "solid"`
- `opacity: 100`
- `strokeColor: "#EEEEEE"` — off-white
- `backgroundColor: "#393E46"` — secondary dark
- `fontFamily: 3` — monospace (Cascadia)
- `fontSize: 16`

### Shapes with Labels

Use inline `label` for text inside shapes — avoids manual text positioning:

```json
{
  "type": "rectangle",
  "id": "api-gateway",
  "x": 60,
  "y": 60,
  "width": 200,
  "height": 80,
  "label": {
    "text": "API Gateway"
  }
}
```

The label inherits element styling. All text renders in monospace off-white.

### Arrows

Arrows use `points` array (offsets from origin) and optional arrowheads:

```json
{
  "type": "arrow",
  "id": "api-to-db",
  "x": 260,
  "y": 100,
  "width": 260,
  "height": 0,
  "points": [[0, 0], [260, 0]],
  "endArrowhead": "arrow",
  "startArrowhead": null,
  "strokeColor": "#EEEEEE",
  "backgroundColor": "transparent"
}
```

For critical/primary paths, use accent teal: `strokeColor: "#00ADB5"`.

### Accent Color Usage

- `#00ADB5` (teal) — sparingly, for focal points: databases, key services, critical paths
- Most elements use `#EEEEEE` stroke on `#393E46` background
- Canvas background: `#222831`

### Layout Spacing

| Property | Value |
|----------|-------|
| Horizontal gap between elements | 260px |
| Vertical gap between rows | 140px |
| Rectangle size | 200 x 80 |
| Ellipse size | 160 x 80 |
| Diamond size | 140 x 140 |
| Canvas edge padding | 60px |

All coordinates must be multiples of 20px for grid alignment.

### Layout Patterns

**Vertical flow** (default for flow/sequence):
- Elements stacked top-to-bottom
- Arrows pointing down
- Decision diamonds branch left/right

**Horizontal pipeline** (data flow, pipelines):
- Elements left-to-right
- Arrows pointing right

**Hub-and-spoke** (architecture with central service):
- Central element in the middle
- Connected services around it

---

## What NOT to Do

- Do NOT use UUIDs for element IDs — use descriptive names
- Do NOT create separate text elements for labels — use the `label` property
- Do NOT exceed 20 elements without suggesting a split
- Do NOT regenerate from scratch on modification unless asked
- Do NOT use gradients, glossy effects, or bright colors — moody, minimal, precise
- Do NOT ignore the current file state when modifying — always read first
