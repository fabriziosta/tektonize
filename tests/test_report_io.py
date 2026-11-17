"""Tests for YAML conversion reports."""

from __future__ import annotations

from pathlib import Path

from tektonize.report_io import CONVERSION_REPORT_NAME, dump_report, load_report, write_report


def test_dump_report_is_yaml_not_json() -> None:
    text = dump_report(
        {
            "stage": "01_raw_ast",
            "failed": 1,
            "failures": [
                {
                    "source_file": "src/Foo.groovy",
                    "errors": [
                        {
                            "message": "Parse error at L1",
                            "details": {
                                "line": 1,
                                "context": ["  1 | class Foo", "> 2 | implements"],
                            },
                        }
                    ],
                }
            ],
        }
    )
    assert "stage: 01_raw_ast" in text
    assert '"stage"' not in text
    assert "failed: 1" in text


def test_write_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / CONVERSION_REPORT_NAME
    payload = {"timestamp": "t", "failed": 0, "stage_summaries": {"05_final": {"failed": 0}}}
    write_report(path, payload)
    assert path.exists()
    assert load_report(path) == payload
