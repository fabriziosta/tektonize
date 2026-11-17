"""End-to-end conversion tests against the neutral sample library."""

from __future__ import annotations

from pathlib import Path

from tektonize.converter import Converter
from tektonize.markers import GROOVY_COMMENT_MARKER, TODO_MARKER
from tektonize.report_io import CONVERSION_REPORT_NAME, load_report

SAMPLE_LIB = Path(__file__).parent / "fixtures" / "sample_lib"


def _count_markers_under(root: Path) -> tuple[int, int]:
    todo = groovy = 0
    if not root.exists():
        return 0, 0
    for py in root.rglob("*.py"):
        if py.name == "__init__.py":
            continue
        text = py.read_text(encoding="utf-8")
        todo += text.count(TODO_MARKER)
        groovy += text.count(GROOVY_COMMENT_MARKER)
    return todo, groovy


class TestConverterE2E:
    def test_batch_conversion_no_crashes(self, tmp_path: Path) -> None:
        """All sample library files should convert without raising exceptions."""
        converter = Converter(
            input_dir=SAMPLE_LIB,
            output_dir=tmp_path,
            stage_filter={"05_final"},
        )
        report = converter.run()

        assert report["total_files"] >= 7
        assert report["failed"] == 0
        assert report["succeeded"] >= 7

    def test_conversion_report_written(self, tmp_path: Path) -> None:
        converter = Converter(
            input_dir=SAMPLE_LIB,
            output_dir=tmp_path,
            stage_filter={"05_final"},
        )
        converter.run()

        report_path = tmp_path / CONVERSION_REPORT_NAME
        assert report_path.exists()

        data = load_report(report_path)
        assert "total_files" in data
        assert "succeeded" in data
        assert "file_results" in data
        assert "stage_summaries" in data
        assert "total_todo_markers" in data
        assert "total_groovy_comments" in data
        assert isinstance(data["total_todo_markers"], int)
        assert isinstance(data["total_groovy_comments"], int)
        assert "05_final" in data["stage_summaries"]
        assert (tmp_path / "05_final" / CONVERSION_REPORT_NAME).exists()

    def test_stage_timings_recorded(self, tmp_path: Path) -> None:
        converter = Converter(
            input_dir=SAMPLE_LIB,
            output_dir=tmp_path,
            stage_filter={"05_final"},
            progress=False,
        )
        report = converter.run()
        timings = report.get("stage_timings_seconds") or {}
        for stage in (
            "01_raw_ast",
            "02_typed_ast",
            "03_transformed",
            "04_emitted",
            "05_final",
            "05_final compile",
        ):
            assert stage in timings
            assert isinstance(timings[stage], (int, float))
            assert timings[stage] >= 0

    def test_marker_totals_count_05_final_only(self, tmp_path: Path) -> None:
        """Report TODO/GROOVY totals must match ``05_final`` on disk, not ``04_emitted``."""
        converter = Converter(input_dir=SAMPLE_LIB, output_dir=tmp_path)
        converter.run()
        data = load_report(tmp_path / CONVERSION_REPORT_NAME)

        final_todo, final_groovy = _count_markers_under(tmp_path / "05_final")
        assert data["total_todo_markers"] == final_todo
        assert data["total_groovy_comments"] == final_groovy

        # Extra markers in 04_emitted must not affect a recount of finals.
        emitted = tmp_path / "04_emitted"
        assert emitted.exists()
        poisoned = next(emitted.rglob("*.py"))
        poisoned.write_text(
            poisoned.read_text(encoding="utf-8")
            + f"\n# {TODO_MARKER}: from intermediate\n{GROOVY_COMMENT_MARKER} x\n",
            encoding="utf-8",
        )
        emitted_todo, emitted_groovy = _count_markers_under(emitted)
        assert emitted_todo > final_todo or emitted_groovy > final_groovy

        recount_todo, recount_groovy = converter._count_markers_in_final([])
        assert recount_todo == final_todo
        assert recount_groovy == final_groovy

    def test_compile_failures_count_as_failed(self, tmp_path: Path) -> None:
        """Invalid Python under 05_final is recorded on the stage report."""
        from tektonize.converter import STAGE_ORDER, ConversionReport, FileResult

        final = tmp_path / "05_final" / "src"
        final.mkdir(parents=True)
        broken = final / "broken.py"
        broken.write_text("def broken(\n", encoding="utf-8")

        converter = Converter(input_dir=tmp_path, output_dir=tmp_path)
        stage_failures = {s: [] for s in STAGE_ORDER}
        report = ConversionReport(
            file_results=[
                FileResult(
                    source_file="Broken.groovy",
                    output_file="src/broken.py",
                    status="success",
                )
            ]
        )
        converter._check_final_compile(report, stage_failures)
        assert stage_failures["05_final"]
        assert any("Compile error" in e.message for e in stage_failures["05_final"][0].errors)

    def test_output_files_created(self, tmp_path: Path) -> None:
        converter = Converter(
            input_dir=SAMPLE_LIB,
            output_dir=tmp_path,
            stage_filter={"05_final"},
        )
        converter.run()

        final_dir = tmp_path / "05_final"
        assert final_dir.exists()

        py_files = list(final_dir.rglob("*.py"))
        assert len(py_files) >= 7

    def test_enum_file_valid_python(self, tmp_path: Path) -> None:
        """The simplest pattern (enums) should produce valid Python."""
        import py_compile

        converter = Converter(
            input_dir=SAMPLE_LIB,
            output_dir=tmp_path,
            stage_filter={"05_final"},
        )
        converter.run()

        enum_file = tmp_path / "05_final/src/mylib/config/deployment_type.py"
        assert enum_file.exists()
        py_compile.compile(str(enum_file), doraise=True)

    def test_single_file_conversion(self, tmp_path: Path) -> None:
        single = SAMPLE_LIB / "src/mylib/config/DeploymentType.groovy"
        converter = Converter(
            input_dir=SAMPLE_LIB,
            output_dir=tmp_path,
            single_file=single,
        )
        report = converter.run()

        assert report["total_files"] == 1
        assert report["succeeded"] == 1

    def test_intermediates_written(self, tmp_path: Path) -> None:
        single = SAMPLE_LIB / "src/mylib/config/DeploymentType.groovy"
        converter = Converter(
            input_dir=SAMPLE_LIB,
            output_dir=tmp_path,
            single_file=single,
            stage_filter={"01_raw_ast", "02_typed_ast", "05_final"},
        )
        converter.run()

        assert (tmp_path / "01_raw_ast").exists()
        assert (tmp_path / "02_typed_ast").exists()
        assert (tmp_path / "05_final").exists()
