"""Converter orchestrator: walks input directory, runs parse → transform → emit pipeline.

Writes intermediate outputs and produces conversion_report.yaml.
"""

from __future__ import annotations

import json
import py_compile
import time
import warnings
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from tektonize import __version__
from tektonize import terminal as term
from tektonize.discovery import is_test_groovy, iter_groovy_sources
from tektonize.emitter.formatting import format_python_source
from tektonize.emitter.python_emitter import PythonEmitter
from tektonize.markers import GROOVY_COMMENT_MARKER, TODO_MARKER
from tektonize.parser.ast_nodes import GroovySourceFile
from tektonize.parser.groovy_ast import GroovyParser, normalize_groovy_for_treesitter
from tektonize.parser.parse_diagnostics import collect_cst_parse_issues
from tektonize.report_io import CONVERSION_REPORT_NAME, write_report
from tektonize.transformer.name_converter import (
    groovy_filename_to_python,
)

STAGE_ORDER = (
    "01_raw_ast",
    "02_typed_ast",
    "03_transformed",
    "04_emitted",
    "05_final",
)
ALL_STAGES = set(STAGE_ORDER)

# Short blurbs for terminal progress / report timings (keep under ~50 chars).
STAGE_DESCRIPTIONS: dict[str, str] = {
    "01_raw_ast": "parse Groovy CST (tree-sitter)",
    "02_typed_ast": "extract typed AST (classes/methods)",
    "03_transformed": "write transform summaries",
    "04_emitted": "emit Python (+ Tekton stubs)",
    "05_final": "format with black, write 05_final/",
    "05_final compile": "py_compile check of 05_final/",
}


def stage_label(name: str) -> str:
    """Return ``name — description`` for progress lines."""
    desc = STAGE_DESCRIPTIONS.get(name)
    return f"{name} — {desc}" if desc else name


@dataclass
class StageError:
    """One stage error: short message plus optional structured details."""

    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageFailure:
    """A single file failure attributed to one pipeline stage."""

    source_file: str = ""
    output_file: str = ""
    errors: list[StageError] = field(default_factory=list)


@dataclass
class StageReport:
    """Failure report for one conversion stage."""

    stage: str = ""
    timestamp: str = ""
    failed: int = 0
    failures: list[StageFailure] = field(default_factory=list)


@dataclass
class FileResult:
    """Result of converting a single file."""

    source_file: str = ""
    output_file: str = ""
    status: str = "success"
    errors: list[str] = field(default_factory=list)
    stage_errors: dict[str, list[str]] = field(default_factory=dict)
    todo_count: int = 0
    groovy_comment_count: int = 0
    classes_found: list[str] = field(default_factory=list)
    methods_found: int = 0


@dataclass
class ConversionReport:
    """Overall conversion report."""

    timestamp: str = ""
    total_files: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_test_files: int = 0
    skipped_tests: list[str] = field(default_factory=list)
    total_todo_markers: int = 0
    total_groovy_comments: int = 0
    file_results: list[FileResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stage_summaries: dict[str, dict[str, Any]] = field(default_factory=dict)
    stage_timings_seconds: dict[str, float] = field(default_factory=dict)


@dataclass
class _FileWork:
    """In-memory state for one Groovy file as it moves through pipeline steps."""

    groovy_file: Path
    relative: Path
    result: FileResult
    source: str = ""
    parsed: GroovySourceFile | None = None
    python_source: str | None = None
    skip_remaining: bool = False


class Converter:
    """Orchestrate the full Groovy → Python conversion pipeline."""

    def __init__(
        self,
        input_dir: Path,
        output_dir: Path,
        stage_filter: set[str] | None = None,
        single_file: Path | None = None,
        *,
        progress: bool = False,
        dsl_hints: dict[str, str] | None = None,
        default_image: str | None = None,
    ) -> None:
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.stage_filter = stage_filter
        self.single_file = single_file
        self.progress = progress
        self.parser = GroovyParser()
        self.emitter = PythonEmitter(dsl_hints=dsl_hints)
        if default_image:
            from tektonize.tekton import task_definition as _task_def

            _task_def.DEFAULT_TASK_IMAGE = default_image
        self._stage_timings: dict[str, float] = {}
        # Widest in-progress line seen (for clean ``\r`` clears across stages).
        self._progress_line_width = 0

    def _should_write_stage(self, stage: str) -> bool:
        if self.stage_filter is None:
            return True
        return stage in self.stage_filter

    def _run_timed_step(self, name: str, fn: Callable[[], None]) -> None:
        """Run ``fn``; when ``progress`` is on, show an in-place status line.

        Elapsed times are recorded for the conversion report table — they are
        not left on the progress lines (that would duplicate the Stages table).
        """
        label = stage_label(name)
        status = f"  {label} ..."
        if self.progress:
            self._progress_line_width = max(self._progress_line_width, len(status) + 8)
            click.echo(f"  {term.info(label)} ...", nl=False)
        t0 = time.perf_counter()
        try:
            fn()
        finally:
            elapsed = time.perf_counter() - t0
            self._stage_timings[name] = round(elapsed, 3)
            if self.progress:
                # Clear the in-progress line; timings appear only in the report.
                click.echo("\r" + (" " * self._progress_line_width) + "\r", nl=False)

    def run(self) -> dict[str, Any]:
        """Run the full conversion pipeline."""
        report = ConversionReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        stage_failures: dict[str, list[StageFailure]] = {s: [] for s in STAGE_ORDER}

        if self.single_file:
            if is_test_groovy(self.single_file, root=self.input_dir):
                report.skipped_test_files = 1
                try:
                    skipped_rel = (
                        self.single_file.resolve().relative_to(self.input_dir.resolve()).as_posix()
                    )
                except ValueError:
                    skipped_rel = self.single_file.name
                report.skipped_tests = [skipped_rel]
                groovy_files: list[Path] = []
            else:
                groovy_files = [self.single_file]
        else:
            groovy_files, report.skipped_tests = iter_groovy_sources(self.input_dir)
            report.skipped_test_files = len(report.skipped_tests)

        report.total_files = len(groovy_files)
        works = [self._make_file_work(gf) for gf in groovy_files]

        if self.progress:
            click.echo(
                f"{term.bold('Converting')} {len(works)} files → {term.path(self.output_dir)}"
            )

        # Always run processing steps (writes gated by stage_filter).
        self._run_timed_step("01_raw_ast", lambda: self._batch_01_raw_ast(works, stage_failures))
        self._run_timed_step(
            "02_typed_ast", lambda: self._batch_02_typed_ast(works, stage_failures)
        )
        self._run_timed_step(
            "03_transformed", lambda: self._batch_03_transformed(works, stage_failures)
        )
        self._run_timed_step("04_emitted", lambda: self._batch_04_emitted(works, stage_failures))
        self._run_timed_step("05_final", lambda: self._batch_05_final(works, stage_failures))

        # Report marker totals from 05_final on disk only (not 01-04 intermediates).
        report.total_todo_markers, report.total_groovy_comments = self._count_markers_in_final(
            works
        )

        for work in works:
            report.file_results.append(work.result)

        # Compile-check all written 05_final Python files
        self._run_timed_step(
            "05_final compile",
            lambda: self._check_final_compile(report, stage_failures),
        )

        # Overall failed = hard failures (emit/format/compile/conversion crash).
        # Parse issues are still listed in per-stage reports but do not alone fail a file.
        hard_stages = frozenset({"04_emitted", "05_final"})
        for result in report.file_results:
            hard = bool(hard_stages.intersection(result.stage_errors)) or result.status == "failed"
            if hard:
                result.status = "failed"
                report.failed += 1
                for stage, errs in result.stage_errors.items():
                    if stage in hard_stages:
                        report.errors.extend(errs)
            else:
                result.status = "success"
                report.succeeded += 1

        report.stage_timings_seconds = dict(self._stage_timings)
        report.stage_summaries = self._write_stage_reports(report.timestamp, stage_failures)
        self._write_report(report)

        return {
            "total_files": report.total_files,
            "succeeded": report.succeeded,
            "failed": report.failed,
            "skipped_test_files": report.skipped_test_files,
            "errors": report.errors,
            "total_todo_markers": report.total_todo_markers,
            "total_groovy_comments": report.total_groovy_comments,
            "stage_summaries": report.stage_summaries,
            "stage_timings_seconds": report.stage_timings_seconds,
        }

    def _make_file_work(self, groovy_file: Path) -> _FileWork:
        try:
            relative = groovy_file.relative_to(self.input_dir)
        except ValueError:
            relative = Path(groovy_file.name)
        return _FileWork(
            groovy_file=groovy_file,
            relative=relative,
            result=FileResult(source_file=str(relative)),
        )

    def _record_stage_error(
        self,
        result: FileResult,
        stage_failures: dict[str, list[StageFailure]],
        stage: str,
        error: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        stage_error = StageError(message=error, details=dict(details or {}))
        result.stage_errors.setdefault(stage, []).append(error)
        result.errors.append(f"[{stage}] {error}")
        # Deduplicate per-file entries in the stage list
        existing = next(
            (f for f in stage_failures[stage] if f.source_file == result.source_file),
            None,
        )
        if existing is None:
            stage_failures[stage].append(
                StageFailure(
                    source_file=result.source_file,
                    output_file=result.output_file,
                    errors=[stage_error],
                )
            )
        else:
            existing.errors.append(stage_error)
            if result.output_file and not existing.output_file:
                existing.output_file = result.output_file

    def _convert_file(
        self,
        groovy_file: Path,
        stage_failures: dict[str, list[StageFailure]],
    ) -> FileResult:
        """Convert a single Groovy file through all stages (test/helper entry)."""
        work = self._make_file_work(groovy_file)
        self._batch_01_raw_ast([work], stage_failures)
        self._batch_02_typed_ast([work], stage_failures)
        self._batch_03_transformed([work], stage_failures)
        self._batch_04_emitted([work], stage_failures)
        self._batch_05_final([work], stage_failures)
        return work.result

    def _batch_01_raw_ast(
        self,
        works: list[_FileWork],
        stage_failures: dict[str, list[StageFailure]],
    ) -> None:
        """Stage 1: CST parse + diagnostics for every file."""
        for work in works:
            if work.skip_remaining:
                continue
            try:
                work.source = work.groovy_file.read_text(encoding="utf-8")
                normalized = normalize_groovy_for_treesitter(work.source)
                raw_tree = self.parser.get_raw_tree(work.source)
                parse_issues = collect_cst_parse_issues(raw_tree, normalized)
                if parse_issues:
                    for issue in parse_issues:
                        self._record_stage_error(
                            work.result,
                            stage_failures,
                            "01_raw_ast",
                            issue.summary(),
                            details=asdict(issue),
                        )
                if self._should_write_stage("01_raw_ast"):
                    self._write_intermediate("01_raw_ast", work.relative, raw_tree, ".json")
            except Exception as e:
                work.result.status = "failed"
                work.skip_remaining = True
                self._record_stage_error(
                    work.result,
                    stage_failures,
                    "01_raw_ast",
                    f"Conversion error: {e}",
                )

    def _batch_02_typed_ast(
        self,
        works: list[_FileWork],
        stage_failures: dict[str, list[StageFailure]],
    ) -> None:
        """Stage 2: typed AST extraction (fallback noted here)."""
        for work in works:
            if work.skip_remaining:
                continue
            try:
                parsed = self.parser.parse_file(work.groovy_file)
                work.parsed = parsed
                typed_ast = self._source_file_to_dict(parsed)
                if parsed.used_fallback:
                    self._record_stage_error(
                        work.result,
                        stage_failures,
                        "02_typed_ast",
                        "Used fallback regex parser due to tree-sitter errors",
                        details=parsed.fallback_details or {"mode": "unknown"},
                    )
                if self._should_write_stage("02_typed_ast"):
                    self._write_intermediate("02_typed_ast", work.relative, typed_ast, ".json")
                work.result.classes_found = [c.name for c in parsed.classes]
                work.result.methods_found = sum(len(c.methods) for c in parsed.classes) + len(
                    parsed.top_level_methods
                )
            except Exception as e:
                work.result.status = "failed"
                work.skip_remaining = True
                self._record_stage_error(
                    work.result,
                    stage_failures,
                    "02_typed_ast",
                    f"Conversion error: {e}",
                )

    def _batch_03_transformed(
        self,
        works: list[_FileWork],
        stage_failures: dict[str, list[StageFailure]],
    ) -> None:
        """Stage 3: write transform summary JSON (emit applies transformers)."""
        del stage_failures  # no stage-3 hard errors today
        if not self._should_write_stage("03_transformed"):
            return
        for work in works:
            if work.skip_remaining or work.parsed is None:
                continue
            parsed = work.parsed
            self._write_intermediate(
                "03_transformed",
                work.relative,
                {
                    "source_file": str(work.relative),
                    "classes": [c.name for c in parsed.classes],
                    "imports": len(parsed.imports),
                    "top_level_methods": [m.name for m in parsed.top_level_methods],
                },
                ".json",
            )

    def _batch_04_emitted(
        self,
        works: list[_FileWork],
        stage_failures: dict[str, list[StageFailure]],
    ) -> None:
        """Stage 4: emit Python source (and optional 04_emitted/ write)."""
        for work in works:
            if work.skip_remaining or work.parsed is None:
                continue
            try:
                python_source = self.emitter.emit_file(work.parsed)
            except Exception as e:
                self._record_stage_error(
                    work.result, stage_failures, "04_emitted", f"Emit error: {e}"
                )
                work.result.status = "failed"
                work.skip_remaining = True
                continue

            work.python_source = python_source
            py_relative = self._groovy_to_python_path(work.relative)
            work.result.output_file = str(py_relative)

            if self._should_write_stage("04_emitted"):
                out_path = self.output_dir / "04_emitted" / py_relative
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(python_source, encoding="utf-8")

    def _batch_05_final(
        self,
        works: list[_FileWork],
        stage_failures: dict[str, list[StageFailure]],
    ) -> None:
        """Stage 5: format and write 05_final/."""
        if not self._should_write_stage("05_final"):
            return
        for work in works:
            if work.skip_remaining or work.python_source is None:
                continue
            py_relative = Path(work.result.output_file)
            try:
                formatted = format_python_source(work.python_source)
            except Exception as e:
                self._record_stage_error(
                    work.result, stage_failures, "05_final", f"Format error: {e}"
                )
                formatted = work.python_source
            out_path = self.output_dir / "05_final" / py_relative
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(formatted, encoding="utf-8")
            # Marker counts come from final on-disk Python only (not 04_emitted).
            work.python_source = formatted
            work.result.todo_count = formatted.count(TODO_MARKER)
            work.result.groovy_comment_count = formatted.count(GROOVY_COMMENT_MARKER)

    def _count_markers_in_final(self, works: list[_FileWork]) -> tuple[int, int]:
        """Count ``TEKTONIZE-TODO`` / ``# GROOVY:`` in ``05_final/**/*.py`` only.

        Intermediate stages are ignored — a marker that might be fixed later in
        the pipeline must not inflate the report totals.
        """
        final_dir = self.output_dir / "05_final"
        if not final_dir.exists():
            return 0, 0

        by_output = {
            w.result.output_file: w for w in works if w.result.output_file and not w.skip_remaining
        }
        total_todo = 0
        total_groovy = 0
        for py_path in sorted(final_dir.rglob("*.py")):
            if py_path.name == "__init__.py":
                continue
            text = py_path.read_text(encoding="utf-8")
            todo = text.count(TODO_MARKER)
            groovy = text.count(GROOVY_COMMENT_MARKER)
            total_todo += todo
            total_groovy += groovy
            rel = str(py_path.relative_to(final_dir))
            work = by_output.get(rel)
            if work is not None:
                work.result.todo_count = todo
                work.result.groovy_comment_count = groovy
        return total_todo, total_groovy

    def _check_final_compile(
        self,
        report: ConversionReport,
        stage_failures: dict[str, list[StageFailure]],
    ) -> None:
        """Compile-check generated Python under 05_final and record failures."""
        final_dir = self.output_dir / "05_final"
        if not final_dir.exists():
            return

        by_source = {fr.source_file: fr for fr in report.file_results}
        by_output = {fr.output_file: fr for fr in report.file_results if fr.output_file}

        for py_path in sorted(final_dir.rglob("*.py")):
            if py_path.name == "__init__.py":
                continue
            rel = str(py_path.relative_to(final_dir))
            result = by_output.get(rel)
            if result is None:
                # Synthesize a result row for orphan outputs
                result = FileResult(source_file=rel, output_file=rel)
                report.file_results.append(result)
                by_source[rel] = result

            try:
                # Emit already sanitized; still silence SyntaxWarning so convert
                # does not spam stderr if a leftover escape slips through.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    py_compile.compile(str(py_path), doraise=True)
            except py_compile.PyCompileError as e:
                msg = str(e).strip().splitlines()[-1][:300]
                self._record_stage_error(
                    result,
                    stage_failures,
                    "05_final",
                    f"Compile error: {msg}",
                )
            except SyntaxError as e:
                self._record_stage_error(
                    result,
                    stage_failures,
                    "05_final",
                    f"Compile error: {e}",
                )

    def _write_stage_reports(
        self,
        timestamp: str,
        stage_failures: dict[str, list[StageFailure]],
    ) -> dict[str, dict[str, Any]]:
        """Write per-stage conversion_report.yaml files; return summaries."""
        summaries: dict[str, dict[str, Any]] = {}
        for stage in STAGE_ORDER:
            failures = stage_failures.get(stage, [])
            # Always write a stage report when the stage dir exists or there are failures
            stage_dir = self.output_dir / stage
            if not failures and not stage_dir.exists() and not self._should_write_stage(stage):
                continue

            stage_report = StageReport(
                stage=stage,
                timestamp=timestamp,
                failed=len(failures),
                failures=failures,
            )
            stage_dir.mkdir(parents=True, exist_ok=True)
            report_rel = f"{stage}/{CONVERSION_REPORT_NAME}"
            write_report(self.output_dir / report_rel, asdict(stage_report))
            summaries[stage] = {
                "failed": stage_report.failed,
                "report": report_rel,
            }
        return summaries

    def _groovy_to_python_path(self, relative: Path) -> Path:
        """Convert a Groovy file path to Python file path."""
        parts = list(relative.parts)

        if parts and parts[-1].endswith(".groovy"):
            parts[-1] = groovy_filename_to_python(parts[-1])

        return Path(*parts)

    def _write_intermediate(
        self,
        stage: str,
        relative: Path,
        data: Any,
        suffix: str,
    ) -> None:
        """Write an intermediate output file."""
        out_relative = relative.with_suffix(suffix)
        out_path = self.output_dir / stage / out_relative
        out_path.parent.mkdir(parents=True, exist_ok=True)

        content = {
            "source_file": str(relative),
            "stage": stage,
            "converter_version": __version__,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        out_path.write_text(json.dumps(content, indent=2, default=str), encoding="utf-8")

    def _source_file_to_dict(self, parsed: GroovySourceFile) -> dict[str, Any]:
        """Convert a GroovySourceFile to a JSON-serializable dict."""
        result: dict[str, Any] = {
            "file_path": parsed.file_path,
            "package": parsed.package,
            "imports": [
                {"path": i.path, "alias": i.alias, "is_static": i.is_static} for i in parsed.imports
            ],
            "classes": [],
            "top_level_methods": [m.name for m in parsed.top_level_methods],
            "parse_errors": parsed.parse_errors,
            "used_fallback": parsed.used_fallback,
            "fallback_details": parsed.fallback_details,
        }

        for cls in parsed.classes:
            cls_dict: dict[str, Any] = {
                "kind": cls.kind.value if hasattr(cls.kind, "value") else str(cls.kind),
                "name": cls.name,
                "fields": [
                    {
                        "name": f.name,
                        "type": str(f.type_ref) if f.type_ref else None,
                        "default": f.default_value,
                    }
                    for f in cls.fields
                ],
                "methods": [
                    {"name": m.name, "params": len(m.parameters), "modifiers": m.modifiers}
                    for m in cls.methods
                ],
            }
            if hasattr(cls, "extends"):
                cls_dict["extends"] = cls.extends
            if hasattr(cls, "implements"):
                cls_dict["implements"] = cls.implements
            if hasattr(cls, "values"):
                cls_dict["values"] = [v.name for v in cls.values]
            result["classes"].append(cls_dict)

        return result

    def _write_report(self, report: ConversionReport) -> None:
        """Write top-level conversion_report.yaml."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        write_report(self.output_dir / CONVERSION_REPORT_NAME, asdict(report))

    def _generate_init_files(self) -> None:
        """Generate __init__.py files for all output packages."""
        final_dir = self.output_dir / "05_final"
        if not final_dir.exists():
            return

        for dirpath in sorted(final_dir.rglob("*")):
            if dirpath.is_dir():
                init_file = dirpath / "__init__.py"
                if not init_file.exists():
                    modules = [
                        f.stem
                        for f in dirpath.iterdir()
                        if f.suffix == ".py" and f.stem != "__init__"
                    ]
                    if modules:
                        from tektonize.transformer.import_transformer import generate_init_py

                        pkg_path = str(dirpath.relative_to(final_dir)).replace("/", ".")
                        init_file.write_text(generate_init_py(pkg_path, modules), encoding="utf-8")
