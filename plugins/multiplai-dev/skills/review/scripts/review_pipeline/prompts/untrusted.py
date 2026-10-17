"""The fence for text a model or another person wrote that goes into a new prompt.

`fenced` defangs the text with `multiplai_core.untrusted` (the fence's own
tags made inert, control and bidi characters dropped) and wraps it in an
`<untrusted-content>` fence attributed to *label*. Markdown fences are kept:
a diff or a report quotes code, and a converter copies quotes from it exactly,
so the text must reach the model as it was written. The prompts around it say
that fenced text is data.

Used for the piece diffs, the path data, the reports passed to convert and the
linked docs. The older `prompts.fence` is left as it is.
"""

from __future__ import annotations

from multiplai_core.untrusted import defang


def fenced(label: str, text: str) -> str:
    source = defang(label, markdown_fences=False).replace('"', "&quot;")
    return f'<untrusted-content source="{source}">\n{defang(text, markdown_fences=False)}\n</untrusted-content>'
