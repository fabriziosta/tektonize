"""Rich diagnostics for tree-sitter Groovy parse ERROR / missing nodes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Lone backslash ERROR tokens (shell line-continuations confused by the grammar).
_BACKSLASH_NOISE = re.compile(r"^\\+\n?$")


@dataclass
class ParseIssue:
    """One CST parse problem with enough context to fix the source."""

    line: int
    column: int
    end_line: int
    end_column: int
    kind: str  # "error" | "missing"
    node_type: str
    text: str
    source_line: str
    caret: str
    context: list[str] = field(default_factory=list)
    child_types: list[str] = field(default_factory=list)
    ancestor_types: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """One-line message suitable for conversion_report.yaml."""
        loc = f"L{self.line}:C{self.column}"
        if self.end_line != self.line or self.end_column != self.column:
            loc = f"{loc}-L{self.end_line}:C{self.end_column}"
        text = self.text.replace("\n", "\\n").strip()
        if len(text) > 60:
            text = text[:57] + "..."
        detail_bits: list[str] = []
        if text and text not in ("ERROR", "MISSING"):
            detail_bits.append(repr(text))
        elif self.child_types:
            detail_bits.append("children=[" + ", ".join(self.child_types[:6]) + "]")
        if self.ancestor_types:
            detail_bits.append("in " + "/".join(self.ancestor_types[-4:]))
        snippet = self.source_line.strip()
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
        msg = f"Parse {self.kind} at {loc}"
        if detail_bits:
            msg += ": " + "; ".join(detail_bits)
        if snippet:
            msg += f" | {snippet}"
        return msg


def collect_cst_parse_issues(
    raw_tree: dict[str, Any],
    source: str,
    *,
    context_lines: int = 2,
) -> list[ParseIssue]:
    """Walk a raw CST dict and return detailed parse issues.

    Filters known tree-sitter-groovy noise that is not actionable for Groovy
    shared libraries (optional semicolons, empty-map ``[:]`` fallout, nested
    ERROR children, lone ``\\`` tokens).
    """
    lines = source.splitlines()
    issues: list[ParseIssue] = []
    seen: set[tuple[int, int, int, int, str]] = set()

    def walk(node: dict[str, Any], ancestors: list[str]) -> None:
        node_type = str(node.get("type", ""))
        is_missing = bool(node.get("is_missing"))
        if node_type == "ERROR" or is_missing:
            issue = _issue_from_node(
                node,
                lines,
                ancestors=ancestors,
                context_lines=context_lines,
                kind="missing" if is_missing and node_type != "ERROR" else "error",
            )
            if not _is_noise_issue(issue):
                key = (
                    issue.line,
                    issue.column,
                    issue.end_line,
                    issue.end_column,
                    issue.kind,
                )
                if key not in seen:
                    seen.add(key)
                    issues.append(issue)
        child_ancestors = [*ancestors, node_type] if node_type else ancestors
        for child in node.get("children", []) or []:
            walk(child, child_ancestors)

    walk(raw_tree, [])
    return issues


def _is_noise_issue(issue: ParseIssue) -> bool:
    """Return True for CST problems that are grammar false positives for Groovy."""
    # Groovy does not require trailing semicolons; the grammar often inserts missing ';'.
    if issue.kind == "missing" and issue.node_type == ";":
        return True

    text = issue.text.strip()
    ancestors = issue.ancestor_types

    # Nested ERROR nodes are cascading fallout from a parent ERROR already reported.
    if "ERROR" in ancestors:
        return True

    # Empty map literal [:] is misparsed as array_literal + stray ':' ERROR.
    if text == ":" and "array_literal" in ancestors:
        return True

    # Shell-style line continuations confused outside string context.
    return bool(_BACKSLASH_NOISE.fullmatch(issue.text) or text in {"\\", "\\\\"})


def _issue_from_node(
    node: dict[str, Any],
    lines: list[str],
    *,
    ancestors: list[str],
    context_lines: int,
    kind: str,
) -> ParseIssue:
    start = node.get("start") or [0, 0]
    end = node.get("end") or start
    start_line = int(start[0]) + 1
    start_col = int(start[1])
    end_line = int(end[0]) + 1
    end_col = int(end[1])

    text = _node_text(node, lines, start_line, start_col, end_line, end_col)
    children = node.get("children") or []
    child_types = [str(c.get("type", "")) for c in children if isinstance(c, dict)]

    source_line = _line_at(lines, start_line)
    caret = _caret_line(source_line, start_col, end_line, end_col, start_line)
    context = _context_block(lines, start_line, context_lines)

    return ParseIssue(
        line=start_line,
        column=start_col,
        end_line=end_line,
        end_column=end_col,
        kind=kind,
        node_type=str(node.get("type", "")),
        text=text,
        source_line=source_line,
        caret=caret,
        context=context,
        child_types=child_types,
        ancestor_types=ancestors[-8:],
    )


def _node_text(
    node: dict[str, Any],
    lines: list[str],
    start_line: int,
    start_col: int,
    end_line: int,
    end_col: int,
) -> str:
    direct = node.get("text")
    if isinstance(direct, str) and direct.strip() and direct != "ERROR":
        return direct

    # Prefer reconstructing from source using the node span.
    sliced = _slice_source(lines, start_line, start_col, end_line, end_col)
    if sliced.strip():
        return sliced

    # Fall back to concatenating descendant leaf text.
    chunks: list[str] = []

    def collect(n: dict[str, Any]) -> None:
        t = n.get("text")
        if isinstance(t, str) and t:
            chunks.append(t)
        for child in n.get("children") or []:
            if isinstance(child, dict):
                collect(child)

    collect(node)
    return "".join(chunks)


def _slice_source(
    lines: list[str],
    start_line: int,
    start_col: int,
    end_line: int,
    end_col: int,
) -> str:
    if not lines or start_line < 1:
        return ""
    if start_line == end_line:
        line = _line_at(lines, start_line)
        return line[start_col:end_col]
    parts: list[str] = []
    first = _line_at(lines, start_line)
    parts.append(first[start_col:])
    for ln in range(start_line + 1, end_line):
        parts.append(_line_at(lines, ln))
    last = _line_at(lines, end_line)
    parts.append(last[:end_col])
    return "\n".join(parts)


def _line_at(lines: list[str], line_no: int) -> str:
    idx = line_no - 1
    if idx < 0 or idx >= len(lines):
        return ""
    return lines[idx]


def _caret_line(
    source_line: str,
    start_col: int,
    end_line: int,
    end_col: int,
    start_line: int,
) -> str:
    if not source_line:
        return ""
    start = max(0, min(start_col, len(source_line)))
    if end_line == start_line:
        end = max(start + 1, min(end_col, len(source_line)))
    else:
        end = len(source_line)
    width = max(1, end - start)
    return (" " * start) + ("^" * width)


def _context_block(lines: list[str], center_line: int, radius: int) -> list[str]:
    if not lines:
        return []
    start = max(1, center_line - radius)
    end = min(len(lines), center_line + radius)
    width = len(str(end))
    block: list[str] = []
    for ln in range(start, end + 1):
        marker = ">" if ln == center_line else " "
        block.append(f"{marker} {ln:>{width}} | {_line_at(lines, ln)}")
    return block
