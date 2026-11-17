"""Optional DSL hint overlays for TEKTONIZE-TODO message text.

Hints refine human-readable TODO wording for library-specific step names.
They do **not** control conversion control flow — structural with*/sh*
matching in ``JenkinsDSLTransformer`` does that.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


def load_dsl_hints(path: Path | None) -> dict[str, str]:
    """Load ``stepName: message`` hints from a YAML or JSON file.

    Returns an empty dict when ``path`` is ``None``. Raises ``ValueError``
    on unsupported shape or extension.
    """
    if path is None:
        return {}
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        data: Any = yaml.safe_load(text)
    elif suffix == ".json":
        data = json.loads(text)
    else:
        raise ValueError(f"dsl-hints file must be .yaml/.yml/.json, got {path.suffix!r}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("dsl-hints root must be a mapping of stepName -> message")
    out: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("dsl-hints entries must be string keys and string values")
        out[key] = value
    return out
