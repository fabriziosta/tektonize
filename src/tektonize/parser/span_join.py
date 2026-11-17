"""Delimiter-aware logical-line joining for method bodies.

Joins unfinished call / map / if-condition spans onto one line before
line-oriented transformers run. Does **not** minify whole methods or flatten
closure/block bodies. Triple-quoted regions keep their newlines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Safety caps — avoid runaway joins on pathological input.
_MAX_JOIN_LINES = 80
_MAX_JOIN_CHARS = 12_000

_BLOCK_OPEN_END = re.compile(
    r"(?:"
    r"\)\s*\{\s*$"  # if (cond) { / foo() {
    r"|\b(?:else|try|finally)\s*\{\s*$"
    r"|\bcatch\s*\([^)]*\)\s*\{\s*$"
    r"|\b(?:if|for|while|switch)\b.*\)\s*\{\s*$"
    r"|^\s*\{\s*$"  # bare method/body opener (should be unwrapped already)
    r")"
)


@dataclass
class _Balance:
    paren: int = 0
    brace: int = 0
    bracket: int = 0


def join_open_spans(text: str) -> str:
    """Join unfinished call/map/if-header spans onto single lines.

    - Unbalanced ``(`` / ``[`` → keep joining
    - Unbalanced ``{`` → join only when it looks like a map/dict span, not a
      block body opener (``) {``, ``else {``, …)
    - Never space-join through triple-quoted regions (newlines preserved)
    - Stop before a line that is only a triple-quote opener when the current
      span is not already inside quotes
    """
    if not text or "\n" not in text:
        return text

    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        buf = lines[i]
        i += 1
        joined = 1

        while i < n and joined < _MAX_JOIN_LINES and len(buf) < _MAX_JOIN_CHARS:
            # Unclosed triple-quote: preserve newlines until closed.
            if _has_unclosed_triple_quote(buf):
                buf = buf + "\n" + lines[i]
                i += 1
                joined += 1
                continue

            if not _needs_continuation(buf):
                break

            nxt = lines[i]
            nxt_stripped = nxt.strip()
            # Do not pull a bare triple-quote opener into a call/map span —
            # that starts an sh / GString body owned by Jenkins DSL.
            if nxt_stripped in ('"""', "'''") or re.match(r"^(\"\"\"|\'\'\')\s*$", nxt_stripped):
                break

            # Skip pure blank lines inside a span (collapse to single space).
            if not nxt_stripped:
                i += 1
                joined += 1
                continue

            buf = buf.rstrip() + " " + nxt.lstrip()
            i += 1
            joined += 1

        out.append(buf)

    return "\n".join(out)


def _needs_continuation(buf: str) -> bool:
    """True when ``buf`` ends mid call / map / if-condition (not block body)."""
    if _has_unclosed_triple_quote(buf):
        return True
    bal = _scan_balance(buf)
    if bal.paren > 0 or bal.bracket > 0:
        return True
    if bal.brace > 0:
        # Completed control/call header + block opener — do not flatten body.
        return not _BLOCK_OPEN_END.search(buf.rstrip())
    return False


def _has_unclosed_triple_quote(text: str) -> bool:
    """Return True if an odd number of triple-quote delimiters is open."""
    i = 0
    n = len(text)
    open_q: str | None = None
    while i < n:
        if open_q is None:
            if text.startswith('"""', i):
                open_q = '"""'
                i += 3
                continue
            if text.startswith("'''", i):
                open_q = "'''"
                i += 3
                continue
            if text.startswith("//", i):
                break  # rest of line is comment
            if text.startswith("/*", i):
                j = text.find("*/", i + 2)
                i = n if j < 0 else j + 2
                continue
            if text[i] in "'\"":
                # Single-line string — skip
                q = text[i]
                i += 1
                while i < n:
                    if text[i] == "\\" and i + 1 < n:
                        i += 2
                        continue
                    if text[i] == q:
                        i += 1
                        break
                    i += 1
                continue
            i += 1
            continue
        # Inside triple quote
        if text.startswith(open_q, i):
            open_q = None
            i += 3
            continue
        i += 1
    return open_q is not None


def _scan_balance(text: str) -> _Balance:
    """Count ``()`` / ``{}`` / ``[]`` depth, ignoring strings and comments."""
    bal = _Balance()
    i = 0
    n = len(text)
    while i < n:
        # Triple quotes
        if text.startswith('"""', i) or text.startswith("'''", i):
            q = text[i : i + 3]
            i += 3
            while i < n:
                if text.startswith(q, i):
                    i += 3
                    break
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            continue
        # Line comment
        if text.startswith("//", i):
            break
        # Block comment
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        # Single / double strings
        if text[i] in "'\"":
            q = text[i]
            i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if text[i] == q:
                    i += 1
                    break
                i += 1
            continue

        c = text[i]
        if c == "(":
            bal.paren += 1
        elif c == ")":
            bal.paren = max(0, bal.paren - 1)
        elif c == "{":
            bal.brace += 1
        elif c == "}":
            bal.brace = max(0, bal.brace - 1)
        elif c == "[":
            bal.bracket += 1
        elif c == "]":
            bal.bracket = max(0, bal.bracket - 1)
        i += 1
    return bal


__all__ = ["join_open_spans"]
