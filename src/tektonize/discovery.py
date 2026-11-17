"""Discover Groovy sources for conversion, excluding test code."""

from __future__ import annotations

from pathlib import Path

# Directory names (case-insensitive) that mark a path as test code.
_TEST_DIR_NAMES = frozenset({"test", "tests"})

# Filename stem suffixes that mark a Groovy file as a test (JUnit / Spock).
_TEST_STEM_SUFFIXES = ("Test", "Tests", "Spec", "TestCase")


def _stem_looks_like_test(stem: str) -> bool:
    return any(stem.endswith(suffix) for suffix in _TEST_STEM_SUFFIXES)


def _path_has_test_dir(parts: tuple[str, ...]) -> bool:
    """True if any path component is a test directory name."""
    return any(part.lower() in _TEST_DIR_NAMES for part in parts)


def find_shared_library_root(start: Path) -> Path | None:
    """Walk upward from ``start`` looking for a Jenkins shared-library root.

    Recognizes a directory that contains a ``vars/`` folder and/or a ``src/``
    folder typical of Jenkins shared libraries.
    """
    cur = start.resolve()
    if cur.is_file():
        cur = cur.parent
    for _ in range(32):
        if (cur / "vars").is_dir() or (cur / "src").is_dir():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def is_test_groovy(path: Path, *, root: Path | None = None) -> bool:
    """Return True if ``path`` is test Groovy and should be excluded from conversion.

    A file is treated as a test when:

    - Its stem ends with ``Test``, ``Tests``, ``Spec``, or ``TestCase``
      (e.g. ``FooSpec.groovy``, ``BarTest.groovy``), or
    - Relative to ``root`` (when given), it lives under a ``test/`` or ``tests/``
      directory (any case). Paths are resolved against ``root`` so a library
      checked out under a parent folder named ``tests`` is not falsely excluded.
    """
    if path.suffix.lower() != ".groovy":
        return False

    if _stem_looks_like_test(path.stem):
        return True

    if root is None:
        return False

    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return False

    # Directory components only — the filename itself is handled above.
    return _path_has_test_dir(relative.parts[:-1])


def iter_groovy_sources(root: Path) -> tuple[list[Path], list[str]]:
    """Return ``(sources, skipped_relative_paths)`` under ``root``.

    Sources are non-test ``*.groovy`` files, sorted. Skipped paths are relative
    to ``root`` (posix-style) for reporting.
    """
    root = root.resolve()
    sources: list[Path] = []
    skipped: list[str] = []
    for path in sorted(root.rglob("*.groovy")):
        try:
            rel = path.resolve().relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        if is_test_groovy(path, root=root):
            skipped.append(rel)
        else:
            sources.append(path)
    return sources, skipped
