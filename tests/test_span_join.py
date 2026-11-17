"""Regression tests for delimiter-aware logical-line spanning."""

from __future__ import annotations

from tektonize.emitter.python_emitter import PythonEmitter
from tektonize.parser.span_join import join_open_spans
from tektonize.transformer.expression_transformer import ExpressionTransformer
from tektonize.transformer.jenkins_dsl_transformer import JenkinsDSLTransformer
from tektonize.transformer.method_transformer import MethodTransformer


class TestJoinOpenSpans:
    def test_multiline_call_with_map_joined(self) -> None:
        src = (
            "steps.uploadResultsToDashboard([\n"
            "    status: 'RUNNING',\n"
            "    buildInfo: foo\n"
            "])\n"
        )
        out = join_open_spans(src)
        assert "\n" not in out.strip() or out.count("\n") == 0
        assert "status:" in out and "buildInfo:" in out
        assert out.count("[") == out.count("]")

    def test_multiline_if_condition_joined_body_not_flattened(self) -> None:
        src = (
            "if (config.testScenario == TestScenario.COLLECTION_EXPORT &&\n"
            "    config.enabled) {\n"
            "    doWork()\n"
            "}\n"
        )
        out = join_open_spans(src)
        lines = [ln for ln in out.split("\n") if ln.strip()]
        # Condition on one line with opening brace; body stays separate.
        assert any("COLLECTION_EXPORT" in ln and "config.enabled" in ln for ln in lines)
        assert any(ln.strip() == "doWork()" for ln in lines)

    def test_triple_quote_newlines_preserved(self) -> None:
        src = 'sh """\n' "set +e\n" "echo hi\n" '"""\n'
        out = join_open_spans(src)
        assert 'sh """\nset +e\necho hi\n"""' in out.replace("\r", "")
        assert "# | " not in out  # joiner only; no TaskStep yet

    def test_bare_triple_quote_after_wrapper_not_joined_into_call(self) -> None:
        """``withX {`` then ``\"\"\"`` must not become ``withX { \"\"\"`` on one line."""
        src = (
            "self.shQuietWithEnv(params, [commandLogName: 'run']) {\n"
            '    """\n'
            "    START=$(date +%s)\n"
            '    """\n'
            "}\n"
        )
        out = join_open_spans(src)
        # Opener may join map args; the """ body opener stays on its own line.
        assert '\n    """\n' in out or '\n"""\n' in out

    def test_else_block_not_flattened(self) -> None:
        src = "else {\n    x = 1\n}\n"
        out = join_open_spans(src)
        assert "x = 1" in out
        assert out.split("\n")[0].strip().startswith("else")


class TestSpanJoinEmitIntegration:
    def test_unwrap_before_join_keeps_method_body_lines(self) -> None:
        """Outer ``{…}`` must not be span-joined into one megline."""
        body = (
            "{\n"
            "    def steps = context.stepsFactory\n"
            "    steps.infraSteps.installCollection()\n"
            "}\n"
        )
        lines = PythonEmitter()._transform_method_body(body)
        joined = "\n".join(lines)
        assert "steps = context.stepsFactory" in joined
        assert "installCollection" in joined
        # Must not glue the two statements onto one GROOVY/TODO megline.
        assert "stepsFactory steps.infra" not in joined
        assert "# GROOVY: steps = context.stepsFactory steps" not in joined

    def test_multiline_map_call_emits_without_map_entry_groovy(self) -> None:
        """Former map_entry GROOVY cascade after join becomes live Python or one TODO."""
        body = (
            "{\n"
            "    steps.uploadResultsToDashboard([\n"
            "        status: 'RUNNING',\n"
            "        buildInfo: foo\n"
            "    ])\n"
            "}\n"
        )
        lines = PythonEmitter()._transform_method_body(body)
        joined = "\n".join(lines)
        groovy_entries = [ln for ln in lines if ln.strip().startswith("# GROOVY:") and '":' in ln]
        assert not groovy_entries, joined
        assert (
            "uploadResultsToDashboard" in joined
            or "call-with" in joined
            or "TEKTONIZE-TODO" in joined
        )

    def test_multiline_if_through_transformers(self) -> None:
        body = "{\n" "    if (flag &&\n" "        other) {\n" '        echo "ok"\n' "    }\n" "}\n"
        spanned = join_open_spans(body)
        expr = ExpressionTransformer().transform(spanned)
        jen = JenkinsDSLTransformer().transform_body(expr)
        lines = MethodTransformer().transform_body(jen)
        joined = "\n".join(lines)
        assert "if " in joined
        # Condition should not leave a dangling ``&&`` line as GROOVY.
        assert "# GROOVY: other)" not in joined
        assert "print" in joined or "echo" in joined or "ok" in joined
