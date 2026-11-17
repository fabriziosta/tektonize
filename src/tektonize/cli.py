"""CLI entry points for tektonize."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

import click

from tektonize import __version__ as _FALLBACK_VERSION
from tektonize import terminal as term
from tektonize.report_io import CONVERSION_REPORT_NAME, load_report

_DEFAULT_OUTPUT_DIR_HELP = "output/<UTC-timestamp>"


def _tool_version() -> str:
    try:
        return pkg_version("tektonize")
    except PackageNotFoundError:
        return _FALLBACK_VERSION


def default_output_dir() -> Path:
    """Return ``output/<UTC-timestamp>`` for the moment the run starts."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("output") / stamp


def _resolve_output_dir(
    *,
    input_dir: Path,
    output_dir: Path | None,
    in_place: bool,
) -> Path:
    """Choose the conversion output root.

    ``--in-place`` writes into ``input_dir`` and overrides ``--output-dir``.
    Otherwise use ``output_dir`` or a fresh timestamped ``output/<UTC>`` path.
    """
    if in_place:
        return input_dir
    if output_dir is not None:
        return output_dir
    return default_output_dir()


@click.group(cls=term.ColorGroup)
@click.version_option(version=_tool_version(), prog_name="tektonize")
def cli() -> None:
    """Convert Jenkins shared-library Groovy to incomplete Tekton tasks and fragments."""


cli.help = (
    f"{term.bold(term.info('tektonize'))} {_tool_version()}\n\n"
    "Convert Jenkins shared-library Groovy to incomplete Tekton tasks and fragments."
)


def _print_counts(succeeded: int, failed: int, *, indent: str = "  ") -> None:
    click.echo(f"{indent}Succeeded: {term.success(str(succeeded))}")
    failed_text = term.error(str(failed)) if failed else term.muted(str(failed))
    click.echo(f"{indent}Failed:    {failed_text}")


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_STAGE_ORDER = (
    "01_raw_ast",
    "02_typed_ast",
    "03_transformed",
    "04_emitted",
    "05_final",
    "05_final compile",
)

# Stages that report hard failures in ``stage_summaries``.
_FAILURE_STAGES = (
    "01_raw_ast",
    "02_typed_ast",
    "03_transformed",
    "04_emitted",
    "05_final",
)


def _visible_len(text: str) -> int:
    """Length of ``text`` with ANSI CSI sequences removed."""
    return len(_ANSI_RE.sub("", text))


def _pad(text: str, width: int, *, align: str = "<") -> str:
    """Pad ``text`` to ``width`` visible columns (ANSI-safe)."""
    pad = max(0, width - _visible_len(text))
    if align == ">":
        return (" " * pad) + text
    return text + (" " * pad)


def _summary_failed(summary: dict[str, object]) -> int:
    """Parse ``failed`` from a stage summary dict."""
    raw = summary.get("failed", 0)
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return 0
    return 0


def _summary_report(summary: dict[str, object], stage: str) -> str:
    raw = summary.get("report")
    if isinstance(raw, str) and raw:
        return raw
    return f"{stage}/{CONVERSION_REPORT_NAME}"


def _print_stage_table(
    timings: dict[str, float | int | str],
    stage_summaries: dict[str, dict[str, object]],
) -> None:
    """Print an ASCII table of stage label / time / failure count."""
    from tektonize.converter import stage_label

    rows: list[tuple[str, str, str]] = []
    total_secs = 0.0
    for stage in _STAGE_ORDER:
        has_time = stage in timings
        summary = stage_summaries.get(stage) if stage in _FAILURE_STAGES else None
        if not has_time and not summary:
            continue
        label = stage_label(stage)
        if has_time:
            secs = float(timings[stage])
            total_secs += secs
            time_cell = f"{secs:.1f}s"
        else:
            time_cell = term.muted("-")
        if summary is not None:
            stage_failed = _summary_failed(summary)
            if stage_failed:
                fail_cell = term.error(str(stage_failed))
                label = term.error(label)
            else:
                fail_cell = term.success("0")
        else:
            fail_cell = term.muted("-")
        rows.append((label, time_cell, fail_cell))

    if not rows:
        return

    if timings:
        rows.append((term.muted("total"), f"{total_secs:.1f}s", term.muted("-")))

    headers = (term.bold("Stage"), term.bold("Time"), term.bold("Failed"))
    w_stage = max(_visible_len(headers[0]), max(_visible_len(r[0]) for r in rows))
    w_time = max(_visible_len(headers[1]), max(_visible_len(r[1]) for r in rows), 4)
    w_fail = max(_visible_len(headers[2]), max(_visible_len(r[2]) for r in rows), 6)

    click.echo(f"\n{term.bold('Stages:')}")
    click.echo(
        f"  {_pad(headers[0], w_stage)}  {_pad(headers[1], w_time, align='>')}  "
        f"{_pad(headers[2], w_fail, align='>')}"
    )
    rule = f"  {'─' * w_stage}  {'─' * w_time}  {'─' * w_fail}"
    click.echo(term.muted(rule))
    for label, time_cell, fail_cell in rows:
        click.echo(
            f"  {_pad(label, w_stage)}  {_pad(time_cell, w_time, align='>')}  "
            f"{_pad(fail_cell, w_fail, align='>')}"
        )


def print_conversion_report(output_dir: Path) -> None:
    """Print the conversion report for ``output_dir`` (same as ``tektonize report``)."""
    report_path = output_dir / CONVERSION_REPORT_NAME
    if not report_path.exists():
        click.echo(f"{term.error('No report found at')} {term.path(report_path)}", err=True)
        raise SystemExit(1)

    data = load_report(report_path)
    failed = int(data.get("failed", 0))
    stamp = term.muted(f"({data.get('timestamp', 'unknown')})")
    click.echo(f"{term.bold('Conversion Report')} {stamp}")
    click.echo(f"  Total files:  {data.get('total_files', 0)}")
    _print_counts(int(data.get("succeeded", 0)), failed)

    skipped_tests = int(data.get("skipped_test_files", 0))
    if skipped_tests:
        click.echo(f"  Skipped tests: {term.muted(str(skipped_tests))}")

    todo_count = int(data.get("total_todo_markers", 0) or 0)
    if todo_count:
        click.echo(f"  TEKTONIZE-TODO markers (05_final): {term.warning(str(todo_count))}")

    groovy_count = int(data.get("total_groovy_comments", 0) or 0)
    if groovy_count:
        click.echo(f"  GROOVY comments (05_final): {term.warning(str(groovy_count))}")

    timings: dict[str, float | int | str] = data.get("stage_timings_seconds") or {}
    raw_summaries = data.get("stage_summaries") or {}
    stage_summaries: dict[str, dict[str, object]] = {
        str(k): v for k, v in raw_summaries.items() if isinstance(v, dict)
    }
    if timings or stage_summaries:
        _print_stage_table(timings, stage_summaries)

    # Expand failed stages with file lists under the table.
    for stage in _FAILURE_STAGES:
        summary = stage_summaries.get(stage)
        if not summary:
            continue
        stage_failed = _summary_failed(summary)
        if stage_failed == 0:
            continue
        stage_report_path = output_dir / _summary_report(summary, stage)
        click.echo(f"\n{term.bold(f'{stage} failures:')}")
        _print_stage_failure_list(stage_report_path, limit=20)

    any_stage_failed = any(_summary_failed(s) > 0 for s in stage_summaries.values())
    if failed > 0 or any_stage_failed:
        click.echo(f"\n{term.bold('Report:')} {term.path(report_path)}")
        for stage, summary in stage_summaries.items():
            if _summary_failed(summary) > 0:
                click.echo(f"  {stage}: {term.path(output_dir / _summary_report(summary, stage))}")


def _error_message(error: object) -> str:
    """Normalize a stage error entry (string or ``{message, details}``) to text."""
    if isinstance(error, dict):
        return str(error.get("message") or "")
    return str(error)


def _print_stage_failure_list(stage_report_path: Path, *, limit: int = 20) -> None:
    """Print a short list of failed files for one stage report."""
    if not stage_report_path.exists():
        return
    stage_data = load_report(stage_report_path)
    failures = stage_data.get("failures") or []
    for failure in failures[:limit]:
        source = failure.get("source_file", "?")
        errors = failure.get("errors") or []
        first = _error_message(errors[0]) if errors else ""
        if len(first) > 100:
            first = first[:97] + "..."
        click.echo(f"    - {term.path(source)}" + (f"  {term.muted(first)}" if first else ""))
    remaining = len(failures) - limit
    if remaining > 0:
        click.echo(f"    {term.muted(f'... and {remaining} more')}")


@cli.command()
@click.option(
    "--input-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Root of the Groovy shared library (e.g. my-jenkins-shared-library/).",
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=(
        f"Directory for conversion outputs (default: {_DEFAULT_OUTPUT_DIR_HELP}). "
        "Ignored when --in-place is set."
    ),
)
@click.option(
    "--in-place",
    is_flag=True,
    default=False,
    help="Write conversion outputs into --input-dir (overrides --output-dir).",
)
@click.option(
    "--stages",
    type=str,
    default=None,
    help="Comma-separated stage names to emit (e.g. 01_raw_ast,05_final).",
)
@click.option(
    "--no-intermediates",
    is_flag=True,
    default=False,
    help="Only produce 05_final/ output, skip intermediate stages.",
)
@click.option(
    "--dsl-hints",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Optional YAML/JSON map of stepName -> TEKTONIZE-TODO message "
        "(wording only; does not change matching)."
    ),
)
@click.option(
    "--default-image",
    type=str,
    default=None,
    help="Default container image for emitted TaskStep (default: ubuntu:22.04).",
)
def convert(
    input_dir: Path,
    output_dir: Path | None,
    in_place: bool,
    stages: str | None,
    no_intermediates: bool,
    dsl_hints: Path | None,
    default_image: str | None,
) -> None:
    """Convert an entire Groovy shared library to Python + Tekton.

    Skips test Groovy (under ``test/`` / ``tests/``, or ``*Test`` / ``*Spec`` names).
    Always prints the conversion report at the end (same as ``tektonize report``).
    """
    from tektonize.converter import Converter
    from tektonize.dsl_hints import load_dsl_hints

    resolved_output = _resolve_output_dir(
        input_dir=input_dir, output_dir=output_dir, in_place=in_place
    )

    stage_filter: set[str] | None = None
    if no_intermediates:
        stage_filter = {"05_final"}
    elif stages:
        stage_filter = {s.strip() for s in stages.split(",")}

    converter = Converter(
        input_dir=input_dir,
        output_dir=resolved_output,
        stage_filter=stage_filter,
        progress=True,
        dsl_hints=load_dsl_hints(dsl_hints),
        default_image=default_image,
    )
    converter.run()
    # Report (incl. Stages table) is the sole summary — no second timing list.
    click.echo("")
    print_conversion_report(resolved_output)


@cli.command("convert-file")
@click.option(
    "--input",
    "input_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to a single .groovy file.",
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=(
        f"Directory for conversion outputs (default: {_DEFAULT_OUTPUT_DIR_HELP}). "
        "Ignored when --in-place is set."
    ),
)
@click.option(
    "--in-place",
    is_flag=True,
    default=False,
    help=(
        "Write conversion outputs into the discovered shared-library root "
        "(overrides --output-dir)."
    ),
)
@click.option(
    "--dsl-hints",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Optional YAML/JSON map of stepName -> TEKTONIZE-TODO message "
        "(wording only; does not change matching)."
    ),
)
@click.option(
    "--default-image",
    type=str,
    default=None,
    help="Default container image for emitted TaskStep (default: ubuntu:22.04).",
)
def convert_file(
    input_file: Path,
    output_dir: Path | None,
    in_place: bool,
    dsl_hints: Path | None,
    default_image: str | None,
) -> None:
    """Convert a single Groovy file to Python + Tekton.

    Always prints the conversion report at the end (same as ``tektonize report``).
    Test Groovy (under ``test/`` / ``tests/``, or ``*Test`` / ``*Spec`` names) is
    excluded — the command exits without converting.
    """
    from tektonize.converter import Converter
    from tektonize.discovery import find_shared_library_root, is_test_groovy
    from tektonize.dsl_hints import load_dsl_hints

    library_root = find_shared_library_root(input_file) or input_file.parent
    if is_test_groovy(input_file, root=library_root):
        click.echo(
            f"{term.error('Skipped test Groovy (not converted):')} {term.path(input_file)}",
            err=True,
        )
        raise SystemExit(1)

    resolved_output = _resolve_output_dir(
        input_dir=library_root, output_dir=output_dir, in_place=in_place
    )

    converter = Converter(
        input_dir=library_root,
        output_dir=resolved_output,
        single_file=input_file,
        progress=True,
        dsl_hints=load_dsl_hints(dsl_hints),
        default_image=default_image,
    )
    result = converter.run()
    if result["failed"] > 0:
        click.echo(
            f"{term.error('Converted with errors:')} "
            f"{term.path(input_file.name)} → {term.path(resolved_output)}"
        )
        click.echo(f"  Errors: {term.error(str(result['errors']))}", err=True)
    else:
        click.echo(
            f"{term.success('Converted:')} "
            f"{term.path(input_file.name)} → {term.path(resolved_output)}"
        )
    click.echo("")
    print_conversion_report(resolved_output)


@cli.command()
@click.option(
    "--output-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Directory containing conversion outputs.",
)
def report(output_dir: Path) -> None:
    """Print conversion report from a previous run."""
    print_conversion_report(output_dir)
