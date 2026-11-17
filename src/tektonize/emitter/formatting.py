"""Formatting utilities for Python code generation."""

from __future__ import annotations


def indent(text: str, level: int = 1, spaces: int = 4) -> str:
    """Indent a block of text by the given level."""
    prefix = " " * (level * spaces)
    lines = text.split("\n")
    return "\n".join(prefix + line if line.strip() else "" for line in lines)


def format_docstring(text: str | None, indent_level: int = 1) -> list[str]:
    """Format a docstring at the given indent level."""
    if not text:
        return []

    prefix = " " * (indent_level * 4)
    lines = text.strip().split("\n")

    if len(lines) == 1 and len(lines[0]) < 72:
        return [f'{prefix}"""{lines[0]}"""']

    result = [f'{prefix}"""']
    for line in lines:
        if line.strip():
            result.append(f"{prefix}{line.strip()}")
        else:
            result.append("")
    result.append(f'{prefix}"""')
    return result


def wrap_line(line: str, max_length: int = 100) -> list[str]:
    """Wrap a long line into multiple lines if needed."""
    if len(line) <= max_length:
        return [line]

    # For now, return as-is — black will handle wrapping
    return [line]


def format_python_source(source: str) -> str:
    """Format Python source with black.

    Falls back to returning the source as-is if black fails.
    """
    try:
        import black

        mode = black.Mode(
            target_versions={black.TargetVersion.PY310},
            line_length=100,
        )
        return black.format_str(source, mode=mode)
    except Exception:
        return source
