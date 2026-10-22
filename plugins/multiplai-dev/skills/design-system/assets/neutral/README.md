# neutral

The design system used when none is configured: greys with one blue accent,
system fonts, no web fonts. Both schemes follow the reader's setting and an
explicit `data-theme="light"|"dark"` on `<html>`.

Every text/background pair the check lists reaches WCAG AA (4.5:1) in light
and in dark. Run `python -m design_system check neutral` to see the ratios.

Copy it to start your own: `python -m design_system create <name>`.
