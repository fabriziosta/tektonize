"""Regression tests for parse / CST diagnostics failures already fixed.

Each test locks a concrete bug that showed up in real conversion reports
(e.g. optional ``;``, empty map ``[:]``, nested ERROR noise, shell ``\\``).
Do not weaken or delete these when changing the parser — update them only if
the intended behavior changes deliberately.
"""

from __future__ import annotations

import py_compile
import re
from pathlib import Path

from tektonize.converter import Converter
from tektonize.parser.groovy_ast import (
    EMPTY_MAP_SENTINEL,
    GroovyParser,
    normalize_groovy_for_treesitter,
)
from tektonize.parser.normalize import EMPTY_LIST_SENTINEL
from tektonize.parser.parse_diagnostics import collect_cst_parse_issues
from tektonize.report_io import CONVERSION_REPORT_NAME, load_report
from tektonize.transformer.expression_transformer import ExpressionTransformer
from tektonize.transformer.method_transformer import MethodTransformer
from tektonize.transformer.type_mapper import TypeMapper

SAMPLE_LIB = Path(__file__).parent / "fixtures" / "sample_lib"


def _issues(source: str) -> list:
    normalized = normalize_groovy_for_treesitter(source)
    return collect_cst_parse_issues(GroovyParser().get_raw_tree(source), normalized)


class TestOptionalSemicolonNoise:
    """tree-sitter-groovy inserts missing ';' after Groovy fields — not actionable."""

    def test_field_declarations_without_semicolon_not_reported(self) -> None:
        source = "class Foo {\n  def x\n  String y = 'a'\n  int z = 1\n}\n"
        assert _issues(source) == []

    def test_missing_semicolon_node_filtered_even_if_present_in_cst(self) -> None:
        source = "class Foo {\n  def x\n}\n"
        # Raw CST still has missing ';' — diagnostics must filter it.
        import tree_sitter_groovy as tsgroovy
        from tree_sitter import Language, Parser

        tree = Parser(Language(tsgroovy.language())).parse(source.encode("utf-8"))
        assert any(n.is_missing and n.type == ";" for n in _walk_nodes(tree.root_node))
        assert _issues(source) == []


class TestEmptyMapLiteral:
    """Empty map ``[:]`` was misparsed as array_literal + ':' ERROR (thousands of hits)."""

    def test_normalize_rewrites_empty_map_to_sentinel(self) -> None:
        assert EMPTY_MAP_SENTINEL in normalize_groovy_for_treesitter("x = [:]")
        assert "[:]" not in normalize_groovy_for_treesitter("x = [:]")

    def test_empty_map_not_reported_as_colon_error(self) -> None:
        source = "def call(Map overrides = [:]) { def x = [:]; return x }\n"
        assert not any(i.text.strip() == ":" for i in _issues(source))

    def test_expression_transformer_maps_sentinel_and_literal_to_dict(self) -> None:
        t = ExpressionTransformer()
        assert "{}" in t.transform("x = [:]")
        assert "{}" in t.transform(f"x = {EMPTY_MAP_SENTINEL}")
        assert EMPTY_MAP_SENTINEL not in t.transform(f"x = {EMPTY_MAP_SENTINEL}")

    def test_type_mapper_maps_sentinel_default(self) -> None:
        mapper = TypeMapper()
        assert mapper.map_default_value("[:]", None) == "{}"
        assert mapper.map_default_value(EMPTY_MAP_SENTINEL, None) == "{}"

    def test_converter_emits_valid_python_for_empty_map_default(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        (lib / "vars").mkdir(parents=True)
        (lib / "vars" / "Pipeline.groovy").write_text(
            "def call(Map overrides = [:]) {\n  def x = [:]\n  return x\n}\n",
            encoding="utf-8",
        )
        out = tmp_path / "out"
        report = Converter(input_dir=lib, output_dir=out, stage_filter={"05_final"}).run()
        assert report["failed"] == 0
        py_files = list((out / "05_final").rglob("*.py"))
        assert py_files
        text = "\n".join(p.read_text(encoding="utf-8") for p in py_files)
        assert "[:]" not in text
        assert EMPTY_MAP_SENTINEL not in text
        assert "{}" in text
        for py in py_files:
            if py.name != "__init__.py":
                py_compile.compile(str(py), doraise=True)


class TestEmptyListLiteral:
    """Empty ``[]`` on fields ERROR'd and swallowed the next field (like ``[:]``)."""

    def test_normalize_rewrites_empty_list_not_array_type(self) -> None:
        norm = normalize_groovy_for_treesitter("List x = []\nString[] y")
        assert EMPTY_LIST_SENTINEL in norm
        assert "String[]" in norm
        assert re.search(r"(?<!\w)\[\]", norm) is None

    def test_field_defaults_not_reported_and_all_fields_extracted(self) -> None:
        source = (
            "class Foo {\n"
            "  List<String> a = []\n"
            "  List<String> b = []\n"
            "  boolean c = true\n"
            "}\n"
        )
        assert not any(i.text.strip() == "[]" for i in _issues(source))
        ast = GroovyParser().parse_source(source, "Foo.groovy")
        names = [f.name for f in ast.classes[0].fields]
        assert names == ["a", "b", "c"]

    def test_type_mapper_maps_list_sentinel(self) -> None:
        assert TypeMapper().map_default_value(EMPTY_LIST_SENTINEL, None) == "[]"


class TestMultilineTernary:
    """``cond\\n ? a\\n : b`` orphaned the ``?`` line after elvis/as rewrites."""

    def test_multiline_ternary_joined(self) -> None:
        source = "x = flag\n    ? a\n    : b\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "flag ?" in norm.replace("  ", " ")
        assert "\n    ?" not in norm
        assert _issues(source) == []


class TestMatchCaseIndent:
    """Groovy brace ``switch``/``case`` absorb to TODO (not half-live match/case)."""

    def test_switch_absorbed_as_todo_compiles(self, tmp_path: Path) -> None:
        body = (
            "{\n"
            "        switch (type) {\n"
            "            case DeploymentType.RPM:\n"
            "                return new RpmDeployment()\n"
            "            default:\n"
            "                throw new IllegalArgumentException('bad')\n"
            "        }\n"
            "}\n"
        )
        lines = MethodTransformer().transform_body(body)
        joined = "\n".join(lines)
        assert "TEKTONIZE-TODO" in joined
        assert "switch/case" in joined
        assert "# GROOVY: case" not in joined
        assert "# GROOVY: switch" not in joined
        # No live match/case residue that would IndentationError under sanitize.
        assert not any(
            ln.strip().startswith("match ") and not ln.strip().startswith("#") for ln in lines
        )
        wrapper = "def create(type):\n" + "\n".join(
            ("    " + ln if ln.strip() else ln) for ln in lines
        )
        path = tmp_path / "match_case.py"
        path.write_text(wrapper + "\n", encoding="utf-8")
        py_compile.compile(str(path), doraise=True)


class TestIfElifElseIndentEscalation:
    """if/elif/else with GROOVY-commented bodies escalated indent per clause."""

    def test_if_elif_else_align_after_groovy_comments(self, tmp_path: Path) -> None:
        """Reproduces fresh_install_scenario.py / rpm_upgrade_scenario.py failures."""
        body = (
            "{\n"
            "        if (provisionerInventory && installerInventory) {\n"
            "            steps.testSuiteSteps.applyLicense([\n"
            "                provisionerInventory: provisionerInventory\n"
            "            ])\n"
            "        } else if (installerInventory) {\n"
            "            steps.testSuiteSteps.applyLicenseNoProvisioner([\n"
            "                installerInventory: installerInventory\n"
            "            ])\n"
            "        } else {\n"
            "            throw new IllegalArgumentException('no inventory')\n"
            "        }\n"
            "}\n"
        )
        lines = MethodTransformer().transform_body(body)
        if_indent = elif_indent = else_indent = None
        for line in lines:
            s = line.strip()
            if s.startswith("if ") and s.endswith(":") and "# " not in s:
                if_indent = len(line) - len(line.lstrip())
            if s.startswith("elif ") and "# " not in s:
                elif_indent = len(line) - len(line.lstrip())
            if s == "else:" and "# " not in line.strip():
                else_indent = len(line) - len(line.lstrip())
        assert if_indent is not None, f"no if found in {lines}"
        assert elif_indent == if_indent, f"elif@{elif_indent} != if@{if_indent}"
        assert else_indent == if_indent, f"else@{else_indent} != if@{if_indent}"
        wrapper = "def test():\n" + "\n".join(("    " + ln if ln.strip() else ln) for ln in lines)
        path = tmp_path / "if_elif_else.py"
        path.write_text(wrapper + "\n", encoding="utf-8")
        py_compile.compile(str(path), doraise=True)

    def test_child_lines_one_level_under_opener(self) -> None:
        """Block body must be exactly one level deeper, not two."""
        body = "{\n" "    if (x) {\n" "            doSomething()\n" "    }\n" "}\n"
        lines = MethodTransformer().transform_body(body)
        for line in lines:
            s = line.strip()
            if s.startswith("if ") and s.endswith(":"):
                if_indent = len(line) - len(line.lstrip())
            elif s and not s.startswith("#"):
                body_indent = len(line) - len(line.lstrip())
                assert body_indent <= if_indent + 4, f"body@{body_indent} > if@{if_indent}+4"


class TestTryExceptFinallyAlignment:
    """except popped the try stack so finally had nothing left — misaligned."""

    def test_try_except_finally_all_align(self, tmp_path: Path) -> None:
        """Reproduces generic_pipeline_builder.py failure."""
        body = (
            "{\n"
            "    try {\n"
            "        stagesClosure()\n"
            "        runClosures(postSuccessClosures)\n"
            "    } catch (Exception e) {\n"
            "        script.currentBuild.result = 'FAILURE'\n"
            "        throw e\n"
            "    } finally {\n"
            "        if (pipelineError) {\n"
            "            runClosures(postFailureClosures)\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        lines = MethodTransformer().transform_body(body)
        try_indent = except_indent = finally_indent = None
        for line in lines:
            s = line.strip()
            if s == "try:":
                try_indent = len(line) - len(line.lstrip())
            if s.startswith("except ") and s.endswith(":"):
                except_indent = len(line) - len(line.lstrip())
            if s == "finally:":
                finally_indent = len(line) - len(line.lstrip())
        assert try_indent is not None, f"no try in {lines}"
        assert except_indent == try_indent, f"except@{except_indent} != try@{try_indent}"
        assert finally_indent == try_indent, f"finally@{finally_indent} != try@{try_indent}"
        wrapper = "def test():\n" + "\n".join(("    " + ln if ln.strip() else ln) for ln in lines)
        path = tmp_path / "try_except_finally.py"
        path.write_text(wrapper + "\n", encoding="utf-8")
        py_compile.compile(str(path), doraise=True)


class TestOrphanedElsePass:
    """else: whose if was GROOVY-commented left orphaned ``pass`` at deep indent."""

    def test_orphan_else_pass_compiles(self, tmp_path: Path) -> None:
        """Reproduces containerized_deployment.py failure."""
        body = (
            "{\n"
            "    if (upgradeConfig.fromReleased) {\n"
            "        installInfo = steps.installer.install(provisionInfo + [\n"
            "            versionVarFile: upgradeConfig.fromVersionVarFile\n"
            "        ])\n"
            "    } else {\n"
            "        fromVarsFile = script.env.WORKSPACE + '/vars.yml'\n"
            "        steps.buildSteps.createVarsFile([\n"
            "            buildInfo: context.fromBuildInfo\n"
            "        ])\n"
            "    }\n"
            "}\n"
        )
        lines = MethodTransformer().transform_body(body)
        # No orphaned ``pass`` at a deeper indent than its surroundings
        for i, line in enumerate(lines):
            if line.strip() == "pass":
                indent = len(line) - len(line.lstrip())
                # Check preceding non-empty, non-comment line
                for j in range(i - 1, -1, -1):
                    prev = lines[j]
                    if prev.strip() and not prev.strip().startswith("#"):
                        prev_indent = len(prev) - len(prev.lstrip())
                        if prev.strip().endswith(":"):
                            assert (
                                indent == prev_indent + 4
                            ), f"pass@{indent} not under opener@{prev_indent}"
                        else:
                            assert (
                                indent <= prev_indent
                            ), f"pass@{indent} deeper than prev@{prev_indent}"
                        break
        wrapper = "def test():\n" + "\n".join(("    " + ln if ln.strip() else ln) for ln in lines)
        path = tmp_path / "orphan_else.py"
        path.write_text(wrapper + "\n", encoding="utf-8")
        py_compile.compile(str(path), doraise=True)

    def test_orphan_else_at_deep_level_does_not_steal_if_match(self) -> None:
        """else: at indent 16 must not match an if at indent 0."""
        body = (
            "{\n"
            "    if (valid) {\n"
            "        doWork()\n"
            "    }\n"
            "    // Groovy: deeply nested else from a different block\n"
            "    if (this.something) {\n"
            "        doOther()\n"
            "    } else {\n"
            "        fallback()\n"
            "    }\n"
            "}\n"
        )
        lines = MethodTransformer().transform_body(body)
        # Should compile — no orphaned or mismatched else
        wrapper = "def test():\n" + "\n".join(("    " + ln if ln.strip() else ln) for ln in lines)
        path_obj = __import__("tempfile").NamedTemporaryFile(suffix=".py", delete=False)
        path_obj.write((wrapper + "\n").encode())
        path_obj.close()
        try:
            py_compile.compile(path_obj.name, doraise=True)
        finally:
            __import__("os").unlink(path_obj.name)


class TestElvisOperator:
    """``?:`` flooded step-1 reports; rewrite to a parseable sentinel call."""

    def test_elvis_not_reported_as_parse_error(self) -> None:
        assert _issues("def x = a ?: b\n") == []

    def test_elvis_with_empty_map_default(self) -> None:
        source = "Map infra = overrides.remove('infra') ?: [:]\n"
        assert _issues(source) == []
        norm = normalize_groovy_for_treesitter(source)
        assert "?: " not in norm and "?:" not in norm

    def test_expression_transformer_expands_elvis_to_or(self) -> None:
        t = ExpressionTransformer()
        # Emit path sees the pre-parse sentinel (not raw ``?:``).
        out = t.transform("def x = __TEKTONIZE_ELVIS__(a, b)")
        assert "a or b" in out
        assert "__TEKTONIZE_ELVIS__" not in out
        assert "?:" not in out


class TestSafeNavigation:
    """``?.`` / ``?.method()`` misparsed as ERROR; rewrite to sentinel calls."""

    def test_safe_nav_property_not_reported(self) -> None:
        assert _issues("if (context.config.tests?.suites) {}\n") == []

    def test_safe_nav_method_not_reported(self) -> None:
        assert _issues("if (!content?.trim()) return [:]\n") == []

    def test_safe_nav_with_elvis(self) -> None:
        assert _issues("return counters.get(name)?.get() ?: 0\n") == []

    def test_elvis_then_safe_nav_not_mangled(self) -> None:
        """``a ?: b?.c`` must not rewrite safe-nav as ``SAFE_NAV__(: b, …)``."""
        source = (
            "k8sNamespace: installInfo.k8sNamespace ?: "
            "installInfo.operatorInstallVars?.k8s_namespace\n"
        )
        assert _issues(source) == []
        norm = normalize_groovy_for_treesitter(source)
        assert "SAFE_NAV__(:" not in norm
        assert "__TEKTONIZE_ELVIS__" in norm
        assert "__TEKTONIZE_SAFE_NAV__" in norm
        # Elvis wraps the safe-nav on the right-hand side.
        assert re.search(
            r"__TEKTONIZE_ELVIS__\([^,]+,\s*__TEKTONIZE_SAFE_NAV__",
            norm,
        )

    def test_expression_transformer_expands_safe_nav(self) -> None:
        t = ExpressionTransformer()
        out = t.transform("x = obj?.prop")
        assert "getattr" in out
        assert "?." not in out

    def test_dynamic_gstring_safe_nav_not_reported(self) -> None:
        """``obj?."${key}"`` (BaseSteps vault inject) was an ERROR token."""
        source = 'def creds = path.inject(vault) { obj, key -> obj?."${key}" }\n'
        norm = normalize_groovy_for_treesitter(source)
        assert '?."' not in norm
        assert "__TEKTONIZE_SAFE_NAV__(obj," in norm.replace(" ", "")
        assert "SAFE_NAV__(key" not in norm
        assert _issues(source) == []


class TestAsCast:
    """Groovy ``as Type`` casts were reported as ERROR spans ending at ``as``."""

    def test_as_map_not_reported(self) -> None:
        assert _issues("def a = params.aiAnalysis as Map\n") == []

    def test_as_generic_not_reported(self) -> None:
        assert _issues("settings.changes = (x ?: []) as List<Map>\n") == []

    def test_as_in_ternary_not_reported(self) -> None:
        source = "Map c = params.cleanup instanceof Map ? params.cleanup as Map : [:]\n"
        assert _issues(source) == []

    def test_expression_transformer_drops_as_cast(self) -> None:
        t = ExpressionTransformer()
        out = t.transform("def a = params.aiAnalysis as Map")
        assert "as Map" not in out
        assert "params.aiAnalysis" in out

    def test_import_alias_not_rewritten_as_cast(self) -> None:
        """``import pkg.Type as Alias`` must stay an import, not ``__TEKTONIZE_AS__``."""
        from tektonize.parser.normalize import AS_CAST_SENTINEL

        source = (
            "import mylib.steps.provisioner.KubernetesSteps as ProvKubernetesSteps\n"
            "import mylib.steps.build.TestSteps as BuildTestSteps\n"
            "class StepsFactory {}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert AS_CAST_SENTINEL not in norm
        assert "import mylib.steps.provisioner.KubernetesSteps as ProvKubernetesSteps" in norm
        assert _issues(source) == []


class TestEmptySingleQuotedString:
    """Empty ``''`` collapses class/field CST into package-span ERRORs."""

    def test_empty_single_quote_rewritten_and_not_reported(self) -> None:
        from tektonize.parser.normalize import EMPTY_STR_SENTINEL

        source = "class Foo { String x = '' }\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "''" not in norm
        assert EMPTY_STR_SENTINEL in norm
        assert _issues(source) == []

    def test_elvis_with_empty_single_quote(self) -> None:
        source = "x = p.foo ?: ''\n"
        assert _issues(source) == []
        norm = normalize_groovy_for_treesitter(source)
        assert "''" not in norm


class TestAssertCommaMessage:
    """``assert expr, \"msg\"`` is misparsed; colon form is accepted."""

    def test_assert_comma_rewritten(self) -> None:
        source = 'assert args.podTemplate, "required"\n'
        norm = normalize_groovy_for_treesitter(source)
        assert " : " in norm or ":" in norm
        assert ", " not in norm.split("assert", 1)[1].split(":", 1)[0]
        assert _issues(source) == []


class TestFieldAtAccess:
    """``this.@field`` private-field access confuses the grammar."""

    def test_field_at_rewritten(self) -> None:
        source = "this.@ns = value\n"
        norm = normalize_groovy_for_treesitter(source)
        assert ".@" not in norm
        assert "this.ns" in norm
        assert _issues(source) == []


class TestSlashyMethodArg:
    """``replaceAll(/pat/, …)`` slashy args need quoting like match operators."""

    def test_replace_all_slashy_not_reported(self) -> None:
        source = "s.replaceAll(/[^a-z-]/, '-')\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "replaceAll(/" not in norm
        assert _issues(source) == []

    def test_slashy_field_assignment_and_ternary_arm(self) -> None:
        source = (
            "class Foo {\n"
            "  static final String PAT = /^[a-z_]+$/\n"
            "}\n"
            "x = flag ? other : /(?!)/\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "=/^" not in norm.replace(" ", "")
        assert ": /" not in norm
        assert _issues(source) == []

    def test_tilde_slashy_case_pattern(self) -> None:
        source = "switch (x) { case ~/foo|bar/: break }\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "~/" not in norm
        assert '~"' in norm


class TestInlineContinueBreak:
    """``if (!x) { continue }`` needs a semicolon like ``{ return }``."""

    def test_continue_block_not_reported(self) -> None:
        source = "for (s in list) { if (!s) { continue } }\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "{ continue; }" in norm
        assert _issues(source) == []


class TestBareAssertTermination:
    """Bare ``assert expr`` before the next statement was line-continued."""

    def test_assert_before_next_statement(self) -> None:
        source = "assert stepParameters.inventoryInput\n        script.writeFile(file: 'x')\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "assert stepParameters.inventoryInput;" in norm
        assert not any("inventoryInput" in (i.text or "") for i in _issues(source))

    def test_assert_before_blank_line_then_statement(self) -> None:
        source = "assert stepParameters.imageToScan\n\n        List args = []\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "assert stepParameters.imageToScan;" in norm
        assert _issues(source) == []


class TestBooleanFieldDefaults:
    """``boolean x = true`` without ``;`` before the next field ERRORs on ``true``."""

    def test_boolean_before_empty_list_field_not_reported(self) -> None:
        source = (
            "class Foo {\n"
            "  boolean registerWithRhsm = true\n"
            "  List<String> files = []\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "= true;" in norm
        assert _issues(source) == []


class TestDottedFieldDefaults:
    """``Type x = Type.CONST`` without ``;`` ERRORs on the dotted constant (CollectionMigrationSettings)."""

    def test_enum_constant_default_before_next_field(self) -> None:
        source = (
            "class Foo {\n"
            "  DeploymentType sourceDeploymentType = DeploymentType.RPM\n"
            "  List<String> jobs = []\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "= DeploymentType.RPM;" in norm
        assert not any(i.text == "DeploymentType.RPM" for i in _issues(source))
        ast = GroovyParser().parse_source(source, "Foo.groovy")
        names = [f.name for f in ast.classes[0].fields]
        assert "sourceDeploymentType" in names
        assert "jobs" in names


class TestMapLiteralSentinelTermination:
    """Sentinel ``;`` must not appear on the last map entry before ``]``."""

    def test_last_map_entry_not_terminated_with_semicolon(self) -> None:
        source = "class Foo {\n" "  def defaults = [\n" "    a: [:],\n" "    b: ''\n" "  ]\n" "}\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "__TEKTONIZE_EMPTY_STR__;" not in norm.split("]")[0]
        assert _issues(source) == []


class TestClosureStatementBodies:
    """Closure bodies with statements need an explicit block for tree-sitter."""

    def test_multiline_closure_with_typed_local_not_reported(self) -> None:
        source = (
            "def run() {\n"
            "  testTypes.each { String testType ->\n"
            "    String stageName = testType\n"
            "    println stageName\n"
            "  }\n"
            "}\n"
        )
        assert _issues(source) == []
        norm = normalize_groovy_for_treesitter(source)
        assert "println(stageName)" in norm
        # Typed locals in closures are stripped for tree-sitter (types are not needed for emit).
        assert "stageName = testType" in norm
        assert "String stageName" not in norm

    def test_single_line_closure_method_call_wrapped(self) -> None:
        assert _issues("list.each { t -> println t }\n") == []


class TestEnumSemicolonNormalize:
    """Last enum constant before members needs ``;`` or tree-sitter misses it."""

    def test_enum_with_ctors_and_fields_extracts_all_values(self) -> None:
        source = (
            "enum Topology {\n"
            "    SAAS('saas'),\n"
            "    AZURE('man-b')\n"
            "\n"
            "    private final String codeName\n"
            "    Topology(String codeName) { this.codeName = codeName }\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "AZURE('man-b');" in norm
        assert "private final String codeName;" in norm
        assert _issues(source) == []
        parsed = GroovyParser().parse_source(source, "Topology.groovy")
        assert parsed.classes
        names = [v.name for v in parsed.classes[0].values]
        assert names == ["SAAS", "AZURE"]


class TestMethodPointer:
    """Groovy ``.&`` method pointers misparsed as binary ``.`` + ``&`` ERROR."""

    def test_method_pointer_not_reported(self) -> None:
        assert _issues("def closures = [builder.&postAlways]\n") == []

    def test_expression_transformer_expands_method_pointer(self) -> None:
        t = ExpressionTransformer()
        out = t.transform('x = __TEKTONIZE_METHOD_PTR__(builder, "cleanup")')
        assert 'getattr(builder, "cleanup")' in out
        assert "__TEKTONIZE_METHOD_PTR__" not in out
        assert ".&" not in out


class TestSlashyRegexLiteral:
    """``==~ /pat/`` slashy literals confused the grammar after match operators."""

    def test_match_operator_with_slashy_regex_not_reported(self) -> None:
        source = "if (!(collectionRepo ==~ " "/^[a-zA-Z0-9_\\-]+\\/[a-zA-Z0-9_\\-\\.]+$/)) {}\n"
        assert _issues(source) == []
        norm = normalize_groovy_for_treesitter(source)
        assert "==~ /" not in norm
        assert '==~ "' in norm


class TestNestedErrorNoise:
    """Child ERROR nodes under a parent ERROR duplicated the same failure."""

    def test_nested_error_children_filtered(self) -> None:
        from tektonize.parser.parse_diagnostics import ParseIssue, _is_noise_issue

        child = ParseIssue(
            line=1,
            column=0,
            end_line=1,
            end_column=1,
            kind="error",
            node_type="ERROR",
            text="?",
            source_line="x = a ?: b",
            caret="^",
            ancestor_types=["ERROR", "binary_expression"],
        )
        assert _is_noise_issue(child)


class TestBackslashNoise:
    """Shell line-continuation ``\\`` tokens confused the grammar outside strings."""

    def test_lone_backslash_error_not_reported(self) -> None:
        # Build a tiny CST fragment that looks like the noisy backslash ERRORs
        from tektonize.parser.parse_diagnostics import ParseIssue, _is_noise_issue

        issue = ParseIssue(
            line=1,
            column=0,
            end_line=1,
            end_column=1,
            kind="error",
            node_type="ERROR",
            text="\\",
            source_line="-i ${inv} \\",
            caret="^",
        )
        assert _is_noise_issue(issue)
        issue2 = ParseIssue(
            line=1,
            column=0,
            end_line=1,
            end_column=2,
            kind="error",
            node_type="ERROR",
            text="\\\\\n",
            source_line="foo \\\\",
            caret="^^",
        )
        assert _is_noise_issue(issue2)


class TestStageReportDetails:
    """Parse issues must carry structured details inside conversion_report.yaml."""

    def test_no_parse_errors_sidecar_files(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        (lib / "src").mkdir(parents=True)
        (lib / "src" / "Broken.groovy").write_text("class Foo implements\n", encoding="utf-8")
        out = tmp_path / "out"
        Converter(input_dir=lib, output_dir=out).run()
        assert not (out / "01_raw_ast" / "parse_errors.json").exists()
        assert not (out / "01_raw_ast" / "parse_errors.txt").exists()

    def test_error_objects_have_message_and_details(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        (lib / "src").mkdir(parents=True)
        (lib / "src" / "Broken.groovy").write_text("class Foo implements\n", encoding="utf-8")
        out = tmp_path / "out"
        Converter(input_dir=lib, output_dir=out).run()
        if not _issues("class Foo implements\n"):
            return
        stage = load_report(out / "01_raw_ast" / CONVERSION_REPORT_NAME)
        assert stage["failed"] >= 1
        err = stage["failures"][0]["errors"][0]
        assert err["message"]
        assert err["details"]["source_line"]
        assert err["details"]["context"]
        assert "line" in err["details"]

    def test_typed_ast_report_does_not_repeat_cst_parse_errors(self, tmp_path: Path) -> None:
        """Stage 02 lists fallback/extraction only — not CST copies from stage 01."""
        lib = tmp_path / "lib"
        (lib / "src").mkdir(parents=True)
        (lib / "src" / "Broken.groovy").write_text("class Foo implements\n", encoding="utf-8")
        out = tmp_path / "out"
        Converter(input_dir=lib, output_dir=out).run()
        if not _issues("class Foo implements\n"):
            return
        stage01 = load_report(out / "01_raw_ast" / CONVERSION_REPORT_NAME)
        assert stage01["failed"] >= 1
        assert any("Parse " in e["message"] for f in stage01["failures"] for e in f["errors"])
        stage02_path = out / "02_typed_ast" / CONVERSION_REPORT_NAME
        if not stage02_path.exists():
            return
        stage02 = load_report(stage02_path)
        for failure in stage02.get("failures") or []:
            for err in failure.get("errors") or []:
                msg = err.get("message") or ""
                assert not msg.startswith("Parse error")
                assert not msg.startswith("Parse missing")


class TestSampleLibEmptyMapRegression:
    """Sample lib uses ``[:]`` defaults — must stay hard-success after normalize."""

    def test_example_pipeline_compiles(self, tmp_path: Path) -> None:
        Converter(input_dir=SAMPLE_LIB, output_dir=tmp_path, stage_filter={"05_final"}).run()
        py = tmp_path / "05_final" / "vars" / "example_pipeline.py"
        if not py.exists():
            # layout may flatten vars/
            matches = list((tmp_path / "05_final").rglob("*example_pipeline.py"))
            assert matches, "expected example_pipeline.py in 05_final"
            py = matches[0]
        text = py.read_text(encoding="utf-8")
        assert EMPTY_MAP_SENTINEL not in text
        assert "[:]" not in text
        py_compile.compile(str(py), doraise=True)


class TestUnmatchedCloseDelimiters:
    """Lines with extra ``)`` / ``}`` must become ``# GROOVY:``."""

    def test_extra_close_paren_commented(self) -> None:
        body = '{\n    return ((lambda _o: getattr(_o, "get")() if _o is not None else None)(counters.get(name))) or 0))\n}\n'
        lines = MethodTransformer().transform_body(body)
        joined = "\n".join(lines)
        assert "# GROOVY:" in joined

    def test_mismatched_brace_in_lambda(self) -> None:
        """Groovy ``.map(lambda it: f\"…{it}\" }.join`` closes string-aware (not ``{it)``)."""
        body = '{\n    form_args_str = formArgs.map(lambda it: f"--form {it}" }.join(" ")\n}\n'
        expr = ExpressionTransformer().transform(body)
        lines = MethodTransformer().transform_body(expr)
        joined = "\n".join(lines)
        assert "{it)" not in joined
        assert "join" in joined
        assert "# GROOVY:" not in joined


class TestGroovyNamedParams:
    """Groovy named-param calls ``func(key: value)`` become Python kwargs."""

    def test_named_param_call_becomes_kwargs(self) -> None:
        body = "{\n    separator(name: 'SEP', sectionHeader: 'Params')\n}\n"
        lines = MethodTransformer().transform_body(body)
        joined = "\n".join(lines)
        assert "separator(name='SEP', sectionHeader='Params')" in joined
        assert "# GROOVY:" not in joined

    def test_sleep_named_params_become_kwargs(self) -> None:
        body = "{\n    sleep(time: 5, unit: 'SECONDS')\n}\n"
        lines = MethodTransformer().transform_body(body)
        joined = "\n".join(lines)
        assert "sleep(time=5, unit='SECONDS')" in joined
        assert "# GROOVY:" not in joined


class TestGroovyAssertFString:
    """``assert x : f"msg"`` must convert to ``assert x, f"msg"``."""

    def test_assert_fstring_converted(self) -> None:
        t = ExpressionTransformer()
        result = t.transform('assert parts.__len__() > 1 : f"Invalid format"')
        assert result == 'assert parts.__len__() > 1, f"Invalid format"'

    def test_assert_plain_string_still_works(self) -> None:
        t = ExpressionTransformer()
        result = t.transform("assert x : 'required'")
        assert result == "assert x, 'required'"


class TestShellFragments:
    """Shell ``\\$`` fragments must be commented out."""

    def test_shell_dollar_sign_commented(self) -> None:
        body = r"{\n    print(\$(( END - START )) > testing_time)\n}\n"
        body = "{\n" + r"    print(\$(( END - START )) > testing_time)" + "\n}\n"
        lines = MethodTransformer().transform_body(body)
        joined = "\n".join(lines)
        assert "# GROOVY:" in joined


class TestGroovyRegexMatch:
    """``=~`` operator rewrites to ``re.search`` (Wave 40)."""

    def test_single_tilde_match_to_re_search(self) -> None:
        body = "{\n    if nvr =~ 'ansible-*':\n        pass\n}\n"
        expr = ExpressionTransformer().transform(body)
        lines = MethodTransformer().transform_body(expr)
        joined = "\n".join(lines)
        assert "re.search(" in joined
        assert "=~" not in joined
        assert "# GROOVY:" not in joined


class TestGroovyMapInListBrackets:
    """Groovy string-keyed maps ``['key': value]`` become Python dicts."""

    def test_string_keyed_map_literal_emitted(self) -> None:
        body = "{\n    results = ['total': 0, 'pass': 0]\n}\n"
        expr = ExpressionTransformer().transform(body)
        lines = MethodTransformer().transform_body(expr)
        joined = "\n".join(lines)
        assert "results = {'total': 0, 'pass': 0}" in joined
        assert "# GROOVY:" not in joined


class TestFieldDefaultMapLiteral:
    """Static ``Map`` field defaults must go through ExpressionTransformer.

    Regression: ``PlatformVersions.PLATFORM_VERSIONS`` was emitted as a raw
    ``[…]`` list, then ``_sanitize_syntax`` commented every entry as GROOVY
    and left ``__TEKTONIZE_EMPTY_*`` sentinels behind.
    """

    def test_nested_map_field_default_is_live_python(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        (lib / "src").mkdir(parents=True)
        groovy = (
            "class VersionMap {\n"
            "    static final Map PLATFORM_VERSIONS = [\n"
            "        'NA': [:],\n"
            "        '1.2.7': [\n"
            "            latest: false,\n"
            "            components: [\n"
            "                controller: [version: '3.8.6', tag: ''],\n"
            "            ],\n"
            "        ],\n"
            "    ]\n"
            "}\n"
        )
        (lib / "src" / "VersionMap.groovy").write_text(groovy, encoding="utf-8")
        out = tmp_path / "out"
        report = Converter(input_dir=lib, output_dir=out, stage_filter={"05_final"}).run()
        assert report["failed"] == 0
        py_files = [p for p in (out / "05_final").rglob("*.py") if p.name != "__init__.py"]
        assert len(py_files) == 1
        text = py_files[0].read_text(encoding="utf-8")
        assert "# GROOVY:" not in text
        assert "__TEKTONIZE_EMPTY_" not in text
        assert "PLATFORM_VERSIONS" in text
        assert "'1.2.7'" in text or '"1.2.7"' in text
        assert "latest" in text
        assert "{" in text and "}" in text
        py_compile.compile(str(py_files[0]), doraise=True)


class TestDotPassKeyword:
    """``obj.pass`` uses a Python keyword as attribute — must be caught."""

    def test_dot_pass_commented(self) -> None:
        body = "{\n    testResults.pass = testResults.total\n}\n"
        lines = MethodTransformer().transform_body(body)
        joined = "\n".join(lines)
        assert "# GROOVY:" in joined


class TestTrailingContinuation:
    """Lines ending with ``+`` or ``\\`` are broken continuations."""

    def test_trailing_plus_commented(self) -> None:
        body = '{\n    x = "hello" +\n}\n'
        lines = MethodTransformer().transform_body(body)
        joined = "\n".join(lines)
        assert "# GROOVY:" in joined


class TestOrphanedCaseClause:
    """``case`` without a ``match`` must be commented out."""

    def test_orphan_case_commented(self) -> None:
        body = (
            "{\n"
            "    # GROOVY: match something:\n"
            "        case 'x':\n"
            "            pass\n"
            "}\n"
        )
        lines = MethodTransformer().transform_body(body)
        case_lines = [ln for ln in lines if "case " in ln and not ln.strip().startswith("#")]
        assert not case_lines, f"orphaned case not commented: {case_lines}"

    def test_match_case_not_orphaned(self) -> None:
        body = (
            "{\n"
            "    match x:\n"
            "        case 'a':\n"
            "            pass\n"
            "        case _:\n"
            "            pass\n"
            "}\n"
        )
        lines = MethodTransformer().transform_body(body)
        case_lines = [ln for ln in lines if ln.strip().startswith("case ") and "# " not in ln]
        assert len(case_lines) >= 1, "valid case was incorrectly orphaned"


class TestPostEmitSanitizer:
    """The compile-and-fix sanitizer must produce compilable output."""

    def test_sanitizer_fixes_orphan_try(self) -> None:
        from tektonize.emitter.python_emitter import PythonEmitter

        source = (
            "def test():\n"
            "    try:\n"
            "        x = 1\n"
            "    # GROOVY: } catch (err) {\n"
            "        print('error')\n"
            "\n"
            "def other():\n"
            "    pass\n"
        )
        emitter = PythonEmitter()
        result = emitter._sanitize_syntax(source)
        compile(result, "<test>", "exec")

    def test_sanitizer_fixes_deep_orphaned_code(self) -> None:
        from tektonize.emitter.python_emitter import PythonEmitter

        source = (
            "def test():\n"
            "    # GROOVY: if hasTriggers:\n"
            "                            spec(cronSpec)\n"
            "                                pass\n"
            "    def real_method():\n"
            "        pass\n"
        )
        emitter = PythonEmitter()
        result = emitter._sanitize_syntax(source)
        compile(result, "<test>", "exec")
        assert "def real_method" in result

    def test_sanitizer_end_to_end_converter(self, tmp_path: Path) -> None:
        """Full pipeline: Groovy with try/catch should produce compilable Python."""
        lib = tmp_path / "lib"
        (lib / "src").mkdir(parents=True)
        groovy = (
            "class Security {\n"
            "    def check() {\n"
            "        try {\n"
            "            script.sh 'run-check'\n"
            "            return [result: 'passed']\n"
            "        } catch (err) {\n"
            '            println "Caught: ${err}"\n'
            "            return [result: 'failed']\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        (lib / "src" / "Security.groovy").write_text(groovy, encoding="utf-8")
        out = tmp_path / "out"
        report = Converter(input_dir=lib, output_dir=out, stage_filter={"05_final"}).run()
        assert report["failed"] == 0
        for py in (out / "05_final").rglob("*.py"):
            if py.name != "__init__.py":
                py_compile.compile(str(py), doraise=True)

    def test_sanitizer_trailing_plus(self) -> None:
        from tektonize.emitter.python_emitter import PythonEmitter

        source = "def test():\n" '    x = "hello" +\n' "    # GROOVY: 'world'\n" "    y = 1\n"
        emitter = PythonEmitter()
        result = emitter._sanitize_syntax(source)
        compile(result, "<test>", "exec")
        assert "y = 1" in result

    def test_sanitizer_clears_invalid_brace_escape_warnings(self) -> None:
        """``f\"\\{VAR}\"`` / ``\\$`` in ``\"\"\"`` bodies must not warn."""
        import warnings

        from tektonize.emitter.python_emitter import PythonEmitter

        source = (
            "def test():\n"
            '    print(f"HTTP status: \\{HTTP}")\n'
            '    check = """\n'
            "    # GROOVY: failures=\\$(grep x)\n"
            '    """\n'
        )
        emitter = PythonEmitter()
        result = emitter._sanitize_syntax(source)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", SyntaxWarning)
            compile(result, "<test>", "exec")
        assert not any(issubclass(w.category, SyntaxWarning) for w in caught)
        assert "\\{" not in result
        assert "\\$" not in result

    def test_sanitizer_comments_return_in_finally(self) -> None:
        import warnings

        from tektonize.emitter.python_emitter import PythonEmitter

        source = (
            "def test():\n" "    try:\n" "        x = 1\n" "    finally:\n" "        return x\n"
        )
        emitter = PythonEmitter()
        result = emitter._sanitize_syntax(source)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", SyntaxWarning)
            compile(result, "<test>", "exec")
        assert not any(issubclass(w.category, SyntaxWarning) for w in caught)
        assert "return x" not in result or "# GROOVY:" in result


class TestMultilineTernaryTwoLine:
    """``cond\\n    ? val : fallback`` (two-line ternary) was not joined."""

    def test_two_line_ternary_joined_and_no_errors(self) -> None:
        source = "settings.x = u.containsKey('key')\n    ? u.get('key') : false\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "\n" not in norm.strip()
        assert "?" in norm and ":" in norm
        assert _issues(source) == []

    def test_chained_two_line_ternaries(self) -> None:
        source = (
            "settings.a = u.containsKey('a')\n"
            "    ? u.get('a') : false\n"
            "settings.b = u.containsKey('b')\n"
            "    ? u.get('b') : false\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        lines = [ln for ln in norm.strip().splitlines() if ln.strip()]
        assert len(lines) == 2, f"expected 2 lines, got {lines}"
        assert _issues(source) == []


class TestMultilineElvisChain:
    """``a\\n    ?: b\\n    ?: c`` multiline elvis was not joined."""

    def test_multiline_elvis_joined(self) -> None:
        source = "String x = a\n    ?: b\n    ?: c\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "?:" not in norm
        assert "__TEKTONIZE_ELVIS__" in norm
        assert _issues(source) == []

    def test_elvis_in_map_value(self) -> None:
        source = "password: this.getHostVar(inv)\n" "    ?: this.getHostVar(inv2)\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "?:" not in norm
        assert _issues(source) == []

    def test_elvis_chain_three_deep(self) -> None:
        source = (
            "String registryVarFile = stepParameters.registryVarFile\n"
            "    ?: paramsWrapper.provisionerParameters.registryVarFile\n"
            "    ?: 'default.yml'\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "?:" not in norm
        assert _issues(source) == []

    def test_elvis_at_end_of_line(self) -> None:
        """``expr ?:\\n    fallback`` — elvis operator at end of preceding line."""
        source = (
            "String registryVarFile = stepParameters.registryVarFile ?:\n"
            "        paramsWrapper.provisionerParameters.registryVarFile\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "?:" not in norm
        assert "__TEKTONIZE_ELVIS__" in norm
        assert _issues(source) == []

    def test_elvis_at_end_in_map_value(self) -> None:
        """Named-param value with elvis at line end (CommonSteps pattern)."""
        source = "password: this.getHostVar(inv) ?:\n" "    this.getHostVar(inv2)\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "?:" not in norm
        assert _issues(source) == []


class TestMultilineDotChain:
    """``expr\\n    .method()`` continuation dots were standalone ERROR."""

    def test_dot_chain_joined(self) -> None:
        source = "String x = a.method()\n    .replace('x', '').replace('y', '-')\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "\n" not in norm.strip()
        assert ".replace" in norm
        assert _issues(source) == []

    def test_three_line_dot_chain(self) -> None:
        source = (
            "String x = apiUrl.replaceFirst('a', '')\n"
            "    .replaceFirst('b', '')\n"
            "    .replaceFirst('c', '')\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "\n" not in norm.strip()
        assert _issues(source) == []

    def test_dot_chain_does_not_join_separate_statements(self) -> None:
        source = "x = foo()\nsomethingElse()\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "\n" in norm.strip(), "separate stmts must not be joined"

    def test_dot_chain_does_not_join_unindented(self) -> None:
        source = "x = foo()\n.bar()\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "\n" in norm.strip(), "unindented dot must not be joined"


class TestForEachColon:
    """``for (def x : list)`` Java-style for-each caused whole-block ERROR."""

    def test_for_each_def_rewritten(self) -> None:
        source = "for (def entry : suites) {\n    doStuff(entry)\n}\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "for (entry in suites)" in norm
        assert _issues(source) == []

    def test_for_each_typed(self) -> None:
        source = "for (String x : list) {\n    println x\n}\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "for (x in list)" in norm
        assert _issues(source) == []

    def test_for_each_qualified_generic(self) -> None:
        source = "for (Map.Entry<String, TestSuite> entry : suites) {\n    go(entry)\n}\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "for (entry in suites)" in norm
        assert _issues(source) == []

    def test_normal_for_loop_untouched(self) -> None:
        source = "for (int i = 0; i < 10; i++) { }\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "for (int i = 0" in norm


class TestCommandStyleInvocations:
    """Jenkins command-style calls without parens flood labeled_statement ERRORs."""

    def test_junit_named_args_wrapped_as_map(self) -> None:
        source = (
            "class X {\n"
            "  def m() {\n"
            "    script.junit allowEmptyResults: true, testResults: xunitFile\n"
            "  }\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "allowEmptyResults: true" in norm
        assert "script.junit([" in norm
        assert _issues(source) == []

    def test_script_sh_gstring_gets_parens(self) -> None:
        source = 'class X { def m() { script.sh "ls ${x}" } }\n'
        norm = normalize_groovy_for_treesitter(source)
        assert 'script.sh("ls ${x}")' in norm
        assert _issues(source) == []

    def test_script_echo_command_style(self) -> None:
        source = 'class X { def m() { this.script.echo "hi ${name}" } }\n'
        norm = normalize_groovy_for_treesitter(source)
        assert 'this.script.echo("hi ${name}")' in norm
        assert _issues(source) == []

    def test_write_json_named_args(self) -> None:
        source = (
            "class X {\n"
            "  def m() {\n"
            "    this.script.writeJSON file: 'out.json', json: payload\n"
            "  }\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "writeJSON([" in norm
        assert _issues(source) == []


class TestClosureIfElseWrap:
    """``if / else`` as the sole closure body needs an explicit block."""

    def test_if_else_inside_each_not_reported(self) -> None:
        source = (
            "suites.each { suite ->\n"
            "    if (suite) {\n"
            "        lines << 'a'\n"
            "    } else {\n"
            "        lines << 'b'\n"
            "    }\n"
            "}\n"
        )
        assert _issues(source) == []


class TestInlineReturnSemicolon:
    """``{ return }`` one-liners need a semicolon for tree-sitter."""

    def test_inline_return_in_closure(self) -> None:
        source = "config.each { k, v -> if (SKIP.contains(k)) { return } }\n"
        norm = normalize_groovy_for_treesitter(source)
        assert "{ return; }" in norm
        assert _issues(source) == []


class TestTypedClosureLocalsStripped:
    """Typed locals inside nested closures confuse the grammar."""

    def test_nested_map_each_with_string_locals(self) -> None:
        source = (
            "tiers.each { Map tier ->\n"
            "    String tierName = tier.name\n"
            "    tier.topologies.each { Map topology ->\n"
            "        String topologyName = topology.name\n"
            "    }\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "String tierName" not in norm
        assert "tierName = tier.name" in norm
        assert _issues(source) == []


class TestSoftParseFailureClusters20261019:
    """Regressions for 01_raw_ast soft failures from output/20261019T083345Z."""

    def test_bare_typed_field_before_constructor(self) -> None:
        source = (
            "class Foo {\n"
            "    Map config\n"
            "\n"
            "    Foo(Map config) {\n"
            "        this.config = config\n"
            "    }\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "Map config;" in norm
        assert _issues(source) == []

    def test_assert_with_trailing_comment(self) -> None:
        source = (
            "class Foo {\n"
            "    def m() {\n"
            "        assert stepParameters.hostVar // example: 'admin_password'\n"
            "        assert stepParameters.inventoryFile\n"
            "        def x = 1\n"
            "    }\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "assert stepParameters.hostVar;" in norm
        assert _issues(source) == []

    def test_assert_backslash_continuation_message(self) -> None:
        source = (
            "class Foo {\n"
            "    def m() {\n"
            "        assert branch.length() <= 75, \\\n"
            "            \"Model branch name '${branch}' is long.\"\n"
            '        script.echo "ok"\n'
            "    }\n"
            "}\n"
        )
        assert _issues(source) == []

    def test_gstring_nested_double_quotes_in_interpolation(self) -> None:
        source = (
            "class Foo {\n"
            "    def m() {\n"
            '        resultMessage += "\\n${RUPEE_ASSOCIATION["SUCCESS"]}  ${n} tests passed"\n'
            '        message += ":tag: ${this.getTagsFromJob().join(", ")}\\n"\n'
            "    }\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "RUPEE_ASSOCIATION['SUCCESS']" in norm
        assert ".join(', ')" in norm
        assert _issues(source) == []

    def test_untyped_method_formals_get_def(self) -> None:
        source = (
            "class Version {\n"
            "    static def printParams(params) {\n"
            '        echo lines.join("\\n")\n'
            "    }\n"
            "    static def validAnsiblePermutation(towerVersion, ansibleVersion) {\n"
            "        return true\n"
            "    }\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "printParams(def params)" in norm
        assert "def towerVersion, def ansibleVersion" in norm
        assert "echo(lines.join" in norm
        assert _issues(source) == []

    def test_multiline_named_build_args(self) -> None:
        source = (
            "def call() {\n"
            "    jobsToBuild.each { String jobName ->\n"
            "        build job: jobName,\n"
            "            parameters: [string(name: 'CI_MESSAGE', value: ciMessageJson)],\n"
            "            propagate: false,\n"
            "            wait: false\n"
            "    }\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "build([job: jobName" in norm
        assert "propagate: false" in norm
        assert _issues(source) == []

    def test_command_style_parallel_and_echo_ternary(self) -> None:
        source = (
            "class Foo {\n"
            "    def m() {\n"
            "        this.script.parallel stages\n"
            "    }\n"
            "}\n"
            "def call() {\n"
            "    echo topologiesToRun == allTopologies ?\n"
            '        "Running all: ${x}" :\n'
            '        "Running selected: ${y}"\n'
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "this.script.parallel(stages)" in norm
        assert "echo(" in norm and "allTopologies ?" in norm
        assert _issues(source) == []

    def test_elvis_stops_before_line_comment(self) -> None:
        source = (
            "class Foo {\n"
            "    def m() {\n"
            '        String buildResult = stepParameters.resultValue ?: "ABORTED"'
            " // groovylint-disable-line UnusedVariable\n"
            "    }\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert '__TEKTONIZE_ELVIS__(stepParameters.resultValue, "ABORTED")' in norm
        assert _issues(source) == []

    def test_quoted_gstring_wrapper_to_concat(self) -> None:
        source = (
            "class Foo {\n"
            "    def m(String k) {\n"
            """        return "'${k.replace("'", "\\\\'")}'"\n"""
            "    }\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert '"\'" + (' in norm
        assert _issues(source) == []

    def test_bare_field_not_applied_to_ctor_last_param(self) -> None:
        """Last typed formal before ``)`` must not gain ``;`` (field terminator)."""
        source = (
            "class Foo {\n"
            "    Foo(\n"
            "        PipelineConfig pipelineConfig,\n"
            "        OcpSteps ocpSteps\n"
            "    ) {\n"
            "        this.x = ocpSteps\n"
            "    }\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "OcpSteps ocpSteps;" not in norm
        assert _issues(source) == []

    def test_assert_colon_message_on_next_line(self) -> None:
        source = (
            "class Foo {\n"
            "    def m() {\n"
            "        assert (a.trim() || b.trim()):\n"
            "            'Either a or b must be provided'\n"
            "        def x = 1\n"
            "    }\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "):;" not in norm
        assert _issues(source) == []

    def test_build_variable_not_positional_command(self) -> None:
        """``if (build != null)`` must not become ``build(!= null)``."""
        source = (
            "class Foo {\n"
            "    def m(def build) {\n"
            "        if (build != null) {\n"
            "            return build\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        norm = normalize_groovy_for_treesitter(source)
        assert "build(!= null)" not in norm
        assert "build != null" in norm
        assert _issues(source) == []


class TestJenkinsParamsBindingsEmit:
    """``params.UPPER_SNAKE`` must stay in output; names harvested unbound (no values)."""

    def test_emit_keeps_params_and_harvests_schema(self, tmp_path: Path) -> None:
        from tektonize.emitter.python_emitter import PythonEmitter

        groovy = tmp_path / "CronTrigger.groovy"
        groovy.write_text(
            "def call() {\n"
            "    def nvr = params.OVERRIDE_NVR ?: ''\n"
            "    def folder = params['OVERRIDE_FOLDER_NAME']\n"
            "    def job = env.JOB_NAME\n"
            "}\n",
            encoding="utf-8",
        )
        parsed = GroovyParser().parse_file(groovy)
        out = PythonEmitter().emit_file(parsed)
        assert "params.OVERRIDE_NVR" in out
        assert "env.JOB_NAME" in out
        assert "from tektonize.tekton import ParamSchema" in out
        assert "_PARAM_SCHEMA = ParamSchema(required=" in out
        assert "OVERRIDE_NVR" in out
        assert "OVERRIDE_FOLDER_NAME" in out
        # Values must never be baked in from a Jenkins controller.
        assert "value=" not in out.split("_PARAM_SCHEMA", 1)[1].split("\n", 1)[0]


def _walk_nodes(node):  # type: ignore[no-untyped-def]
    yield node
    for child in node.children:
        yield from _walk_nodes(child)
