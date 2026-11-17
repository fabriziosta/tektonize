"""Markers written into converted output for incomplete / manual follow-up work.

Use ``TEKTONIZE-TODO`` (not a bare ``TODO``) so converted trees are easy to search
without colliding with ordinary repository TODOs:

    rg 'TEKTONIZE-TODO'

Unconverted Groovy body lines that still look like Groovy (or fail validity)
are preserved as ``# GROOVY: …`` comments (search: ``rg '# GROOVY:'``).
"""

from __future__ import annotations

# Distinctive token — uncommon in other codebases; safe to grep globally.
TODO_MARKER = "TEKTONIZE-TODO"

# Full prefix as it appears after ``# `` in generated Python comments.
TODO_PREFIX = f"{TODO_MARKER}:"

# Comment marker for lines left as unconverted Groovy (includes leading ``# ``).
GROOVY_COMMENT_MARKER = "# GROOVY:"


def todo_text(message: str) -> str:
    """Return ``TEKTONIZE-TODO: <message>`` (no leading ``#``)."""
    msg = message.strip()
    if msg.startswith(TODO_PREFIX):
        return msg
    return f"{TODO_PREFIX} {msg}"


def todo_comment(message: str, *, indent: str = "") -> str:
    """Format a single-line tektonize TODO comment: ``# TEKTONIZE-TODO: ...``."""
    return f"{indent}# {todo_text(message)}"
