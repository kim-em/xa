"""A very small mustache subset, for prompt templates.

Prompts are markdown files a human edits, so the syntax has to be forgiving and
obvious. This supports exactly what a prompt needs and nothing more:

    {{name}}              a value, with dotted paths: {{evidence.branch}}
    {{#items}}...{{/items}}   repeat over a list, or render once if truthy
    {{^items}}...{{/items}}   render only when absent or empty
    {{.}}                 the current item, inside a list of scalars

Deliberately not a real template engine. A prompt that needs logic is a sign
the monitor should have computed something and put it in `evidence`.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

TOKEN = re.compile(r"\{\{([#^/]?)([a-zA-Z0-9_.]+|\.)\}\}")


def lookup(stack: Sequence[Any], path: str) -> Any:
    """Resolve a dotted path against the context stack, innermost frame first."""
    if path == ".":
        return stack[-1]
    head, _, rest = path.partition(".")
    for frame in reversed(stack):
        if isinstance(frame, dict) and head in frame:
            value = frame[head]
            for part in rest.split(".") if rest else []:
                if not isinstance(value, dict):
                    return None
                value = value.get(part)
            return value
    return None


def _truthy(value: Any) -> bool:
    return bool(value) and value != "0"


# A section tag alone on its line should not contribute a blank line to the
# output. Without this, every loop in a markdown prompt double-spaces.
STANDALONE = re.compile(r"(?m)^[ \t]*(\{\{[#^/][a-zA-Z0-9_.]+\}\})[ \t]*\r?\n")


def strip_standalone(template: str) -> str:
    return STANDALONE.sub(r"\1", template)


def render(template: str, context: dict[str, Any], _pre: bool = True) -> str:
    if _pre:
        template = strip_standalone(template)
    out: list[str] = []
    stack: list[Any] = [context]
    pos = 0
    # (tag, start_of_body, emitting) for each open section
    sections: list[tuple[str, int, bool]] = []

    while True:
        match = TOKEN.search(template, pos)
        if match is None:
            out.append(template[pos:])
            break

        out.append(template[pos : match.start()])
        sigil, name = match.group(1), match.group(2)
        pos = match.end()

        if sigil in ("#", "^"):
            value = lookup(stack, name)
            body_start = pos
            end = _find_close(template, name, pos)
            body = template[body_start:end]
            pos = end + len(f"{{{{/{name}}}}}")

            if sigil == "^":
                if not _truthy(value):
                    out.append(render_frame(body, stack, context))
                continue

            if isinstance(value, list):
                for entry in value:
                    out.append(render_frame(body, stack + [entry], context))
            elif isinstance(value, dict):
                if value:
                    out.append(render_frame(body, stack + [value], context))
            elif _truthy(value):
                # Push the scalar, so `{{.}}` in the body means the value that
                # opened the section. Rendering the body against the unchanged
                # stack instead left `{{.}}` resolving to the root context, and
                # a count rendered as the entire item: 44KB of engine state,
                # `plan` included, pasted into an agent's prompt.
                out.append(render_frame(body, stack + [value], context))
            continue

        if sigil == "/":
            continue  # unbalanced close; ignore rather than fail a prompt

        value = lookup(stack, name)
        # A dict has no useful string form, and the one Python gives is a dump
        # of whatever happens to be in scope. Prompts are handed to agents, so
        # the safe reading of a mistake here is nothing at all.
        out.append("" if value is None or isinstance(value, (dict, list)) else str(value))

    return "".join(out)


def render_frame(body: str, stack: list[Any], root: dict[str, Any]) -> str:
    """Render a section body with an explicit context stack."""
    merged: dict[str, Any] = {}
    for frame in stack:
        if isinstance(frame, dict):
            merged.update(frame)
    scalar = stack[-1] if not isinstance(stack[-1], dict) else None
    text = render(body, merged, _pre=False)
    if scalar is not None:
        text = text.replace("{{.}}", str(scalar))
        # `{{.}}` is consumed by `render` above when the frame is a dict, so
        # handle the scalar case by substituting before and after.
        text = render(body.replace("{{.}}", str(scalar)), merged, _pre=False)
    return text


def _find_close(template: str, name: str, start: int) -> int:
    """Index of the matching `{{/name}}`, honouring nesting of the same tag."""
    close = f"{{{{/{name}}}}}"
    opens = (f"{{{{#{name}}}}}", f"{{{{^{name}}}}}")
    depth = 0
    i = start
    while i < len(template):
        nxt_close = template.find(close, i)
        if nxt_close == -1:
            return len(template)
        nxt_open = min(
            [p for p in (template.find(o, i) for o in opens) if p != -1 and p < nxt_close],
            default=-1,
        )
        if nxt_open == -1:
            if depth == 0:
                return nxt_close
            depth -= 1
            i = nxt_close + len(close)
        else:
            depth += 1
            i = nxt_open + 1
    return len(template)
