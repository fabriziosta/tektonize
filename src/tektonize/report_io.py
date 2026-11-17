"""Read/write conversion reports as YAML (human-readable stage summaries)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONVERSION_REPORT_NAME = "conversion_report.yaml"


def conversion_report_path(directory: Path, *, stage: str | None = None) -> Path:
    """Path to a top-level or per-stage conversion report."""
    if stage:
        return directory / stage / CONVERSION_REPORT_NAME
    return directory / CONVERSION_REPORT_NAME


def dump_report(data: Any) -> str:
    """Serialize a report dict/dataclass-asdict payload to YAML text."""
    text = yaml.dump(
        data,
        Dumper=_ReportDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )
    return str(text)


def load_report(path: Path) -> dict[str, Any]:
    """Load a conversion report YAML file."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected mapping in {path}, got {type(loaded).__name__}")
    return loaded


def write_report(path: Path, data: Any) -> None:
    """Write ``data`` as YAML to ``path`` (parents created)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_report(data), encoding="utf-8")


class _ReportDumper(yaml.SafeDumper):
    """Prefer readable scalars for long / multi-line report strings."""


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.Node:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    if len(data) > 88:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=">")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_ReportDumper.add_representer(str, _represent_str)
