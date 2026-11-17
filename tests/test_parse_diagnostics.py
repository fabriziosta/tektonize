"""Tests for rich CST parse diagnostics."""

from __future__ import annotations

from pathlib import Path

from tektonize.converter import Converter
from tektonize.parser.groovy_ast import GroovyParser, normalize_groovy_for_treesitter
from tektonize.parser.parse_diagnostics import collect_cst_parse_issues
from tektonize.report_io import CONVERSION_REPORT_NAME, load_report
from tektonize.transformer.expression_transformer import ExpressionTransformer

# Incomplete implements clause — real ERROR (not optional-semicolon noise)
_BROKEN = "class Foo implements\n"


def test_issue_includes_source_line_and_span() -> None:
    source = _BROKEN
    raw = GroovyParser().get_raw_tree(source)
    issues = collect_cst_parse_issues(raw, normalize_groovy_for_treesitter(source))
    assert issues
    issue = issues[0]
    assert issue.line >= 1
    assert issue.context
    summary = issue.summary()
    assert "Parse" in summary
    assert "L" in summary
    assert summary.split(":")[-1].strip() != "ERROR"


def test_optional_semicolon_is_not_reported() -> None:
    source = "class Foo {\n  def x\n  String y = 'a'\n}\n"
    raw = GroovyParser().get_raw_tree(source)
    issues = collect_cst_parse_issues(raw, normalize_groovy_for_treesitter(source))
    assert not any(i.kind == "missing" and i.node_type == ";" for i in issues)
    # Field-only classes should not surface semicolon noise as failures
    assert issues == []


def test_empty_map_literal_normalized_and_not_reported() -> None:
    source = "def call(Map overrides = [:]) { return [:] }\n"
    normalized = normalize_groovy_for_treesitter(source)
    assert "__TEKTONIZE_EMPTY_MAP__" in normalized
    assert "[:]" not in normalized
    raw = GroovyParser().get_raw_tree(source)
    issues = collect_cst_parse_issues(raw, normalized)
    assert not any(i.text.strip() == ":" for i in issues)
    py = ExpressionTransformer().transform(normalized)
    assert "[:]" not in py
    assert "__TEKTONIZE_EMPTY_MAP__" not in py
    assert "{}" in py


def test_nested_error_children_not_reported_separately() -> None:
    # Nonsense ``???`` yields a parent ERROR with nested ERROR children.
    source = "def x = a ??? b\n"
    raw = GroovyParser().get_raw_tree(source)
    issues = collect_cst_parse_issues(raw, normalize_groovy_for_treesitter(source))
    assert issues  # parent ERROR remains
    assert not any("ERROR" in i.ancestor_types for i in issues)


def test_converter_embeds_parse_details_in_stage_report(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    (lib / "src").mkdir(parents=True)
    (lib / "src" / "Broken.groovy").write_text(_BROKEN, encoding="utf-8")

    out = tmp_path / "out"
    Converter(input_dir=lib, output_dir=out).run()

    normalized = normalize_groovy_for_treesitter(_BROKEN)
    if not collect_cst_parse_issues(GroovyParser().get_raw_tree(_BROKEN), normalized):
        return

    stage_report = load_report(out / "01_raw_ast" / CONVERSION_REPORT_NAME)
    assert stage_report["failed"] >= 1
    first_error = stage_report["failures"][0]["errors"][0]
    assert isinstance(first_error, dict)
    assert first_error.get("message")
    details = first_error.get("details") or {}
    assert details.get("source_line")
    assert details.get("context")
    assert "line" in details
