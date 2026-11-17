"""CLI smoke tests."""

from __future__ import annotations

import re
from pathlib import Path

from click.testing import CliRunner

from tektonize import __version__
from tektonize.cli import cli, default_output_dir
from tektonize.report_io import CONVERSION_REPORT_NAME, write_report

_OUTPUT_TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")


def test_help_prints_version() -> None:
    runner = CliRunner()
    for flag in ("--help", "-h", "-help"):
        result = runner.invoke(cli, [flag])
        assert result.exit_code == 0, result.output
        assert f"tektonize {__version__}" in result.output


def test_version_option() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_default_output_dir_is_timestamped_under_output() -> None:
    path = default_output_dir()
    assert path.parts == ("output", path.name)
    assert _OUTPUT_TIMESTAMP_RE.match(path.name)


def test_convert_help_shows_timestamped_default() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["convert", "--help"])
    assert result.exit_code == 0, result.output
    assert "output/<UTC-timestamp>" in result.output
    assert "--in-place" in result.output


def test_resolve_output_dir_in_place() -> None:
    from tektonize.cli import _resolve_output_dir

    inp = Path("/tmp/lib")
    assert _resolve_output_dir(input_dir=inp, output_dir=None, in_place=True) == inp
    assert _resolve_output_dir(input_dir=inp, output_dir=Path("/tmp/out"), in_place=True) == inp
    assert _resolve_output_dir(input_dir=inp, output_dir=Path("/tmp/out"), in_place=False) == Path(
        "/tmp/out"
    )
    defaulted = _resolve_output_dir(input_dir=inp, output_dir=None, in_place=False)
    assert defaulted.parts[0] == "output"
    assert _OUTPUT_TIMESTAMP_RE.match(defaulted.name)


def test_help_colors_options_and_commands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"], color=True)
    assert result.exit_code == 0, result.output
    assert "\x1b[" in result.output
    # Option names (cyan) and command names (green) are styled.
    assert "convert" in result.output
    assert "--version" in result.output


def test_help_plain_without_color() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"], color=False)
    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.output
    assert "Commands:" in result.output
    assert "convert" in result.output


def _write_sample_report(output_dir: Path, *, failed: int = 0) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    stage_summaries = {
        "05_final": {
            "failed": failed,
            "report": f"05_final/{CONVERSION_REPORT_NAME}",
        }
    }
    write_report(
        output_dir / CONVERSION_REPORT_NAME,
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "total_files": 2,
            "succeeded": 2 - failed,
            "failed": failed,
            "total_todo_markers": 3,
            "total_groovy_comments": 5,
            "stage_summaries": stage_summaries,
            "stage_timings_seconds": {
                "05_final": 1.5,
                "05_final compile": 0.1,
            },
        },
    )
    if failed:
        write_report(
            output_dir / "05_final" / CONVERSION_REPORT_NAME,
            {
                "stage": "05_final",
                "failed": failed,
                "failures": [
                    {
                        "source_file": "src/example.groovy",
                        "output_file": "src/example.py",
                        "errors": ["Compile error: invalid syntax"],
                    }
                ],
            },
        )


def test_report_uses_color_when_enabled(tmp_path: Path) -> None:
    _write_sample_report(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["report", "--output-dir", str(tmp_path)], color=True)
    assert result.exit_code == 0, result.output
    assert "\x1b[" in result.output
    assert "Conversion Report" in result.output
    assert "TEKTONIZE-TODO markers (05_final):" in result.output
    assert "GROOVY comments (05_final):" in result.output


def test_report_plain_when_color_disabled(tmp_path: Path) -> None:
    _write_sample_report(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["report", "--output-dir", str(tmp_path)], color=False)
    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.output
    assert "Conversion Report" in result.output
    assert "TEKTONIZE-TODO markers (05_final): 3" in result.output
    assert "GROOVY comments (05_final): 5" in result.output
    assert "Stages:" in result.output
    assert "Time" in result.output and "Failed" in result.output
    assert "1.5s" in result.output
    assert "total" in result.output


def test_report_shows_paths_when_failed(tmp_path: Path) -> None:
    _write_sample_report(tmp_path, failed=1)
    runner = CliRunner()
    result = runner.invoke(cli, ["report", "--output-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Failed:" in result.output
    assert CONVERSION_REPORT_NAME in result.output
    assert "Stages:" in result.output
    assert "Failed" in result.output
    assert "05_final failures:" in result.output
    assert "src/example.groovy" in result.output


def test_convert_file_prints_report_at_end(tmp_path: Path) -> None:
    fixture = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "sample_lib"
        / "src"
        / "mylib"
        / "config"
        / "DeploymentType.groovy"
    )
    out = tmp_path / "out"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["convert-file", "--input", str(fixture), "--output-dir", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "Converted:" in result.output
    assert "Conversion Report" in result.output
    assert result.output.index("Converted:") < result.output.index("Conversion Report")


def test_convert_in_place_writes_into_input_dir(tmp_path: Path) -> None:
    """``--in-place`` writes stage dirs into --input-dir, not ./output/…."""
    import shutil

    lib = tmp_path / "lib"
    shutil.copytree(Path(__file__).resolve().parent / "fixtures" / "sample_lib", lib)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["convert", "--input-dir", str(lib), "--in-place", "--no-intermediates"],
    )
    assert result.exit_code == 0, result.output
    assert (lib / "05_final").is_dir()
    assert (lib / CONVERSION_REPORT_NAME).is_file()
    assert not (tmp_path / "output").exists()
    assert "Converting" in result.output
    assert str(lib) in result.output
