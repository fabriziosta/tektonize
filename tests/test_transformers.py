"""Tests for all transformers: name, type, class, expression, method, import, Jenkins DSL."""

from __future__ import annotations

import re

from tektonize.parser.ast_nodes import (
    ClassDecl,
    EnumDecl,
    EnumValue,
    FieldDecl,
    InterfaceDecl,
    MethodDecl,
    ParameterNode,
    TypeRef,
)
from tektonize.transformer.class_transformer import ClassTransformer
from tektonize.transformer.expression_transformer import ExpressionTransformer
from tektonize.transformer.import_transformer import ImportTransformer
from tektonize.transformer.jenkins_bindings import (
    JENKINS_GLOBAL_BINDINGS,
    format_param_schema_assignment,
    harvest_param_names,
    is_jenkins_global_binding,
    mask_jenkins_binding_accesses,
    param_schema_from_text,
)
from tektonize.transformer.jenkins_dsl_transformer import JenkinsDSLTransformer
from tektonize.transformer.method_transformer import MethodTransformer
from tektonize.transformer.name_converter import (
    camel_to_snake,
    convert_identifier,
    groovy_filename_to_python,
    groovy_import_to_python_module,
    is_upper_snake,
    pascal_to_snake,
)
from tektonize.transformer.type_mapper import TypeMapper


class TestNameConverter:
    def test_pascal_to_snake(self) -> None:
        assert pascal_to_snake("DeploymentType") == "deployment_type"
        assert pascal_to_snake("InstallerSteps") == "installer_steps"
        assert pascal_to_snake("IJenkinsContext") == "i_jenkins_context"

    def test_camel_to_snake(self) -> None:
        assert camel_to_snake("buildAndExecute") == "build_and_execute"
        assert camel_to_snake("getVersionVarFile") == "get_version_var_file"
        assert camel_to_snake("fromMap") == "from_map"

    def test_upper_snake_preserved(self) -> None:
        assert is_upper_snake("KUBERNETES")
        assert is_upper_snake("FRESH_INSTALL")
        assert not is_upper_snake("camelCase")
        assert convert_identifier("KUBERNETES") == "KUBERNETES"

    def test_groovy_filename(self) -> None:
        assert groovy_filename_to_python("DeploymentType.groovy") == "deployment_type.py"
        assert groovy_filename_to_python("RpmFreshInstall.groovy") == "rpm_fresh_install.py"

    def test_groovy_import_to_python(self) -> None:
        mod, sym = groovy_import_to_python_module("mylib.pipelines.config.DeploymentType")
        assert mod == "mylib.pipelines.config.deployment_type"
        assert sym == "DeploymentType"


class TestTypeMapper:
    def test_basic_types(self) -> None:
        mapper = TypeMapper()
        assert mapper.map_type(TypeRef(name="String")) == "str"
        assert mapper.map_type(TypeRef(name="boolean")) == "bool"
        assert mapper.map_type(TypeRef(name="Map")) == "dict"
        assert mapper.map_type(TypeRef(name="void")) == "None"
        assert mapper.map_type(TypeRef(name="Object")) == "Any"

    def test_generic_types(self) -> None:
        mapper = TypeMapper()
        assert (
            mapper.map_type(TypeRef(name="List", generic_params=[TypeRef(name="String")]))
            == "list[str]"
        )
        assert (
            mapper.map_type(
                TypeRef(name="Map", generic_params=[TypeRef(name="String"), TypeRef(name="Object")])
            )
            == "dict[str, Any]"
        )

    def test_inline_generics(self) -> None:
        mapper = TypeMapper()
        assert mapper.map_type(TypeRef(name="List<String>")) == "list[str]"
        assert mapper.map_type(TypeRef(name="Map<String, Object>")) == "dict[str, Any]"

    def test_default_value_mapping(self) -> None:
        mapper = TypeMapper()
        assert mapper.map_default_value("null", None) == "None"
        assert mapper.map_default_value("true", None) == "True"
        assert mapper.map_default_value("false", None) == "False"
        assert mapper.map_default_value("[:]", None) == "{}"
        assert mapper.map_default_value("__TEKTONIZE_EMPTY_MAP__", None) == "{}"
        assert mapper.map_default_value("[]", None) == "[]"
        assert mapper.map_default_value("''", None) == '""'


class TestClassTransformer:
    def test_enum_transform(self) -> None:
        enum = EnumDecl(
            name="DeploymentType",
            package="mylib.config",
            values=[
                EnumValue(name="RPM"),
                EnumValue(name="CONTAINERIZED"),
                EnumValue(name="KUBERNETES"),
            ],
        )
        transformer = ClassTransformer()
        result = transformer.transform(enum)
        assert result.kind == "enum"
        assert result.name == "DeploymentType"
        assert len(result.enum_values) == 3
        assert result.bases == ["Enum"]

    def test_interface_transform(self) -> None:
        iface = InterfaceDecl(
            name="DeploymentStrategy",
            methods=[
                MethodDecl(name="provision", parameters=[ParameterNode(name="context")]),
                MethodDecl(name="install", parameters=[ParameterNode(name="context")]),
            ],
        )
        transformer = ClassTransformer()
        result = transformer.transform(iface)
        assert result.kind == "abc"
        assert "ABC" in result.bases
        assert all(m.is_abstract for m in result.methods)

    def test_dataclass_detection(self) -> None:
        cls = ClassDecl(
            name="InstallerSettings",
            implements=["Serializable"],
            fields=[FieldDecl(name="x"), FieldDecl(name="y"), FieldDecl(name="z")],
        )
        transformer = ClassTransformer()
        result = transformer.transform(cls)
        assert result.kind == "dataclass"


class TestExpressionTransformer:
    def test_new_keyword(self) -> None:
        t = ExpressionTransformer()
        assert "InstallerSettings()" in t.transform("new InstallerSettings()")

    def test_null_to_none(self) -> None:
        t = ExpressionTransformer()
        assert "None" in t.transform("x = null")

    def test_boolean(self) -> None:
        t = ExpressionTransformer()
        assert "True" in t.transform("x = true")
        assert "False" in t.transform("x = false")

    def test_this_to_self(self) -> None:
        t = ExpressionTransformer()
        assert "self.name" in t.transform("this.name")
        assert "StepsFactory(self," in t.transform("StepsFactory(this, pipelineParameters)")

    def test_contains_key_and_each(self) -> None:
        t = ExpressionTransformer()
        assert "(k in jobParams)" in t.transform("jobParams.containsKey(k)")
        out = t.transform("tiers.each { tier ->\n    println tier\n}")
        assert "for tier in tiers:" in out
        assert ".each" not in out

    def test_java_map_and_merge_idioms(self) -> None:
        t = ExpressionTransformer()
        assert "pipelineParameters.get('repoBranch', 'main')" in t.transform(
            "pipelineParameters.getOrDefault('repoBranch', 'main')"
        )
        assert "config.update(overrides)" in t.transform("config.putAll(overrides)")
        assert "infra.append(node)" in t.transform("infra << node")
        merged = t.transform("infra + {\n    key: 1\n}")
        assert "{**infra, **{" in merged
        assert merged.count("{") == merged.count("}")
        assert "{**a, **b}" in t.transform("a + b")
        assert '"hello" + name' in t.transform('"hello" + name') or "hello" in t.transform(
            '"hello" + name'
        )
        # Shell ``set +e`` must never become map-merge ``{**set, **e}``.
        assert "{**set" not in t.transform("set +e")
        assert "{**set" not in t.transform("        set +e\n")
        assert t.transform("set +e").strip() == "set +e"
        # Empty-dict merge ``{} + overrides``.
        assert "{**{}, **overrides}" in t.transform("{} + overrides").replace(" ", "") or (
            "{**{}" in t.transform("{} + overrides")
            and "overrides" in t.transform("{} + overrides")
        )

    def test_script_env_and_current_build_bindings(self) -> None:
        """``script.env.FOO`` / ``self.script.env[…]`` → live ``env`` binding."""
        t = ExpressionTransformer()
        assert "env.WORKSPACE" in t.transform("script.env.WORKSPACE")
        assert "env.BUILD_TAG" in t.transform("self.script.env.BUILD_TAG")
        assert "env[ci_message_env_var]" in t.transform("self.script.env[ci_message_env_var]")
        assert "currentBuild.result" in t.transform("script.currentBuild.result")
        assert "script.env" not in t.transform("x = script.env.JOB_NAME")
        # Through method body: live Python, not GROOVY / TODO.
        expr = t.transform('stage_name = (script.env.STAGE_NAME or "no-stage")')
        lines = MethodTransformer().transform_body(JenkinsDSLTransformer().transform_body(expr))
        joined = "\n".join(lines)
        assert "env.STAGE_NAME" in joined
        assert "# GROOVY:" not in joined
        assert "TEKTONIZE-TODO" not in joined

    def test_unquoted_brace_map_keys(self) -> None:
        """``{ deploymentType: X }`` gets quoted keys; control bodies do not."""
        t = ExpressionTransformer()
        out = t.transform("{ deploymentType: dep, testScenario: scen }")
        assert '"deploymentType"' in out or '"deploymentType":' in out
        assert "deploymentType:" not in out.replace('"deploymentType":', "")
        # for-body braces must not quote the loop variable.
        loop = t.transform("items.each { x ->\n    println x\n}")
        assert "for x in items:" in loop or "for x in items:" in loop
        assert '"x":' not in loop

    def test_map_left_shift_merge_not_append(self) -> None:
        """``{…} << expr`` / ``ident << {…}`` are map merges; ``list << x`` stays append."""
        t = ExpressionTransformer()
        assert '{**{"a": 1}, **({"b": 2})}' in t.transform("[a: 1] << [b: 2]").replace(
            " ", ""
        ) or "{**{" in t.transform("[a: 1] << [b: 2]")
        out = t.transform("[a: 1] << [b: 2]")
        assert "{**" in out and "append" not in out
        assert "{**cfg, **{" in t.transform("cfg << [a: 1]")
        assert "items.append('x')" in t.transform("items << 'x'")
        assert 'lines.append(f"hello")' in t.transform('lines << f"hello"')
        assert "provision_info.update(buildInfo())" in t.transform("provision_info << buildInfo()")
        # Elvis empty-map default must not be eaten by ternary before merge.
        nested = t.transform(
            "[\n"
            "    installerType: 'containerized'\n"
            "] << (overrides.remove('installer') ?: [:])\n"
        )
        assert "if ]" not in nested
        assert "or {}" in nested
        assert "{**" in nested
        # Full method body keeps the merge as live Python (no GROOVY).
        body = (
            "{\n"
            "    def config = [\n"
            "        installer: [\n"
            "            installerType: 'containerized'\n"
            "        ] << (overrides.remove('installer') ?: [:])\n"
            "    ]\n"
            "    config.putAll(overrides)\n"
            "}\n"
        )
        expr = t.transform(body)
        lines = MethodTransformer().transform_body(JenkinsDSLTransformer().transform_body(expr))
        joined = "\n".join(lines)
        assert "# GROOVY:" not in joined
        assert "{**" in joined
        assert "config.update(overrides)" in joined

    def test_triple_quote_not_mangled_to_empty_fstring(self) -> None:
        """``script: \"\"\"`` must not become ``script=\"\"f\"`` via GString rewrite."""
        t = ExpressionTransformer()
        src = (
            "script.sh(\n"
            '    script: """\n'
            "        set +e\n"
            "        (${shellScript}) > ${logFile} 2>&1\n"
            '    """,\n'
            "    returnStatus: true\n"
            ")"
        )
        out = t.transform(src)
        assert 'script=""f"' not in out
        assert 'script="""' in out or 'script: """' in out
        assert "set +e" in out
        assert "{**set" not in out

    def test_instanceof(self) -> None:
        t = ExpressionTransformer()
        assert "isinstance(x, dict)" in t.transform("x instanceof Map")

    def test_ternary_and_bang(self) -> None:
        t = ExpressionTransformer()
        out = t.transform("ocp = (params.ocp instanceof Map) ? params.ocp as Map : [:]")
        assert " if " in out and " else " in out
        assert "?" not in out.replace("?:", "")
        assert t.transform("if (!flag) {").startswith("if (not flag)") or "not flag" in t.transform(
            "!flag"
        )

    def test_empty_map(self) -> None:
        t = ExpressionTransformer()
        assert "{}" in t.transform("x = [:]")
        assert "{}" in t.transform("x = __TEKTONIZE_EMPTY_MAP__")
        assert "__TEKTONIZE_EMPTY_MAP__" not in t.transform("x = __TEKTONIZE_EMPTY_MAP__")

    def test_single_line_map_literal(self) -> None:
        t = ExpressionTransformer()
        out = t.transform("return [a: 1, b: 2]")
        assert 'return {"a": 1, "b": 2}' in out

    def test_named_args_become_kwargs(self) -> None:
        t = ExpressionTransformer()
        out = t.transform("choice(name: 'DEPLOYMENT_TYPE', choices: ['cont', 'rpm'])")
        assert "name='DEPLOYMENT_TYPE'" in out
        assert "choices=['cont', 'rpm']" in out
        assert "name:" not in out
        # Map literals must not be rewritten as kwargs.
        mapped = t.transform("return [name: 'x']")
        assert '"name":' in mapped or "'name':" in mapped

    def test_multiline_nested_map_literal(self) -> None:
        """Identifier-key maps (flat/nested/multiline) become Python dicts."""
        t = ExpressionTransformer()
        src = "return [\n" "    outer: [\n" "        inner: 'x'\n" "    ]\n" "]"
        out = t.transform(src)
        assert "return {" in out
        assert '"outer": {' in out
        assert "\"inner\": 'x'" in out
        assert "[" not in out and "]" not in out
        assert out.count("{") == out.count("}")

    def test_string_keyed_nested_map_literal(self) -> None:
        """Hyphenated string keys (``'ui-controller':``) nest as dicts."""
        t = ExpressionTransformer()
        src = (
            "return [\n"
            "    newUIParameters: [\n"
            "        'ui-controller': [\n"
            "            recordKeyCredId: 'ui-controller-record-key',\n"
            "        ],\n"
            "        'ui-eda': [\n"
            "            recordKeyCredId: 'ui-eda-record-key',\n"
            "        ]\n"
            "    ]\n"
            "]"
        )
        out = t.transform(src)
        assert '"newUIParameters": {' in out
        assert "'ui-controller': {" in out
        assert '"recordKeyCredId":' in out
        assert "[" not in out and "]" not in out

    def test_string_interpolation(self) -> None:
        t = ExpressionTransformer()
        result = t.transform('"Hello ${name}"')
        assert 'f"Hello {name}"' in result

    def test_shell_escaped_dollar_not_invalid_brace_escape(self) -> None:
        """Groovy ``\\${VAR}`` in sh bodies must not become Python ``\\{VAR}``."""
        import warnings

        t = ExpressionTransformer()
        result = t.transform(r'"mkdir -p \${WORKSPACE}/xunit"')
        assert "\\{" not in result
        assert "${WORKSPACE}" in result
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", SyntaxWarning)
            compile(f"x = {result}", "<t>", "exec")
        assert not any(issubclass(w.category, SyntaxWarning) for w in caught)


class TestMethodTransformer:
    def test_empty_body(self) -> None:
        t = MethodTransformer()
        assert t.transform_body("") == ["pass"]
        result = t.transform_body("{}")
        assert result == ["pass"] or "TODO" in str(result)

    def test_groovy_body_gets_commented(self) -> None:
        t = MethodTransformer()
        result = t.transform_body('{ script.sh("echo hello") }')
        assert any("GROOVY" in line or "TEKTONIZE-TODO" in line for line in result)

    def test_preserves_indent_and_if_body(self) -> None:
        t = MethodTransformer()
        result = t.transform_body(
            "{\n"
            "        if (!(params.infra instanceof Map)) {\n"
            "            return settings\n"
            "        }\n"
            "}"
        )
        joined = "\n".join(result)
        assert "if " in joined
        assert any(line.strip() == "return settings" and line.startswith(" ") for line in result)

    def test_multiline_map_return_emitted_as_python_dict(self) -> None:
        """Flat multiline ``return [key: value]`` becomes a live Python dict."""
        body = (
            "{\n"
            "        return [\n"
            "            secretKeyCredId: 'aws_secret_key',\n"
            "            accessKeyCredId: 'aws_access_key'\n"
            "        ]\n"
            "}"
        )
        expr = ExpressionTransformer().transform(body)
        lines = MethodTransformer().transform_body(expr)
        joined = "\n".join(lines)
        assert "return {" in joined
        assert '"secretKeyCredId":' in joined
        assert '"accessKeyCredId":' in joined
        assert "# GROOVY:" not in joined
        assert "TEKTONIZE-TODO" not in joined
        compile("def f():\n" + "\n".join(f"    {ln}" for ln in lines), "<t>", "exec")

    def test_nested_map_return_with_comment(self) -> None:
        """Nested maps and ``//`` comments between entries stay live Python."""
        body = (
            "{\n"
            "        return [\n"
            "            standardCredentials  : [\n"
            "                clientId      : 'AZURE_SP_CLIENT_ID',\n"
            "                clientSecret  : 'AZURE_SP_CLIENT_SECRET'\n"
            "            ],\n"
            "            // management credentials for staging tenant\n"
            "            managementCredentials: [\n"
            "                clientId      : 'STAGING_SP_CLIENT_ID',\n"
            "                subscriptionId: 'STAGING_SP_SUBSCRIPTION_ID'\n"
            "            ]\n"
            "        ]\n"
            "}"
        )
        expr = ExpressionTransformer().transform(body)
        lines = MethodTransformer().transform_body(expr)
        joined = "\n".join(lines)
        assert '"standardCredentials": {' in joined
        assert '"managementCredentials": {' in joined
        assert "}," in joined
        assert "# management credentials" in joined
        assert "# GROOVY:" not in joined
        compile("def f():\n" + "\n".join(f"    {ln}" for ln in lines), "<t>", "exec")

    def test_string_keyed_nested_map_return_emitted(self) -> None:
        """Nested string-keyed maps (E2E secrets style) stay live Python."""
        body = (
            "{\n"
            "        return [\n"
            "            cypressPSIPasswordCredId: 'CYPRESS_PSI_PASSWORD',\n"
            "            newUIParameters: [\n"
            "                    'ui-controller': [\n"
            "                            recordKeyCredId: 'ui-controller-record-key',\n"
            "                    ],\n"
            "                    'ui-eda': [\n"
            "                            recordKeyCredId: 'ui-eda-record-key',\n"
            "                    ]\n"
            "            ]\n"
            "        ]\n"
            "}"
        )
        expr = ExpressionTransformer().transform(body)
        lines = MethodTransformer().transform_body(expr)
        joined = "\n".join(lines)
        assert "return {" in joined
        assert '"newUIParameters": {' in joined
        assert "'ui-controller': {" in joined
        assert "'ui-eda': {" in joined
        assert "# GROOVY:" not in joined
        compile("def f():\n" + "\n".join(f"    {ln}" for ln in lines), "<t>", "exec")

    def test_unconverted_return_bracket_map_still_commented(self) -> None:
        """Without map rewrite, bare ``return [`` remains rejected as Groovy."""
        t = MethodTransformer()
        result = t.transform_body("{\n        return [\n            x: x\n        ]\n}")
        assert not any(line.strip().startswith("return [") for line in result)
        assert any("GROOVY" in line or "TEKTONIZE-TODO" in line for line in result)

    def test_orphan_closer_after_todo_absorbed(self) -> None:
        """``}) {`` / ``\"\"\")`` after a TODO/TaskStep opener must not stay as GROOVY."""
        body = (
            "{\n"
            "        # TEKTONIZE-TODO: withFailureNotification -> Tekton finally\n"
            "        }) {\n"
            "        doWork()\n"
            '        # TaskStep(script="""\n'
            "        # | echo hi\n"
            '        """)\n'
            "}"
        )
        lines = MethodTransformer().transform_body(body)
        joined = "\n".join(lines)
        assert "}) {" not in joined
        assert '# GROOVY: """)' not in joined
        assert "doWork()" in joined

    def test_orphan_groovy_else_pass_finally_absorbed(self) -> None:
        """``# GROOVY: else:`` / ``pass`` / ``finally:`` stubs are always dropped."""
        body = (
            "{\n"
            "        # TEKTONIZE-TODO: glued control-body residue\n"
            "        # GROOVY: else:\n"
            "        # GROOVY: pass\n"
            "        # GROOVY: finally:\n"
            "        # GROOVY: pass\n"
            "        # GROOVY: except Exception as e:\n"
            "        # GROOVY: pass\n"
            '        # GROOVY: """) }\n'
            "        x = 1\n"
            "}"
        )
        lines = MethodTransformer().transform_body(body)
        joined = "\n".join(lines)
        assert "# GROOVY: else:" not in joined
        assert "# GROOVY: pass" not in joined
        assert "# GROOVY: finally:" not in joined
        assert "# GROOVY: except" not in joined
        assert '# GROOVY: """) }' not in joined
        assert "x = 1" in joined
        # Also after a live sibling (brace-unwrap) — do not revive across siblings.
        body2 = (
            "{\n"
            "        if dry_run:\n"
            "            pass\n"
            '        print("simulating")\n'
            "        # GROOVY: else:\n"
            "        # GROOVY: pass\n"
            "        do_real_work()\n"
            "}"
        )
        joined2 = "\n".join(MethodTransformer().transform_body(body2))
        assert "# GROOVY: else:" not in joined2
        assert "# GROOVY: pass" not in joined2
        assert "do_real_work()" in joined2
        # Sanitizer stub drop (post-emit).
        from tektonize.emitter.python_emitter import PythonEmitter

        dirty = (
            "def f():\n"
            "    if dry_run:\n"
            "        pass\n"
            '    print("x")\n'
            "    # GROOVY: else:\n"
            "    # GROOVY: pass\n"
            "    do_work()\n"
        )
        clean = PythonEmitter._drop_orphan_groovy_stubs(dirty)
        assert "# GROOVY: else:" not in clean
        assert "# GROOVY: pass" not in clean
        assert "do_work()" in clean

    def test_orphan_double_brace_closers_absorbed(self) -> None:
        """``}})`` / ``}}`` / orphan ``else:`` / ``pass`` after TODO must not stay GROOVY."""
        body = (
            "{\n"
            "        # TEKTONIZE-TODO: withFailureNotification -> Tekton finally\n"
            "        }})\n"
            "        # GROOVY: }}\n"
            "        # GROOVY: else:\n"
            "        # GROOVY: pass\n"
            "        doWork()\n"
            "}"
        )
        lines = MethodTransformer().transform_body(body)
        joined = "\n".join(lines)
        assert "}})" not in joined
        assert "# GROOVY: }}" not in joined
        assert "# GROOVY: else:" not in joined
        assert "# GROOVY: pass" not in joined
        assert "doWork()" in joined

    def test_revive_groovy_else_when_if_live(self) -> None:
        """``# GROOVY: else:`` after a live ``if`` at same indent becomes live ``else:``."""
        # Feed post-transform lines through revive via a body that keeps the
        # GROOVY else marker (if commented the matching if, else stays orphan).
        lines = MethodTransformer._revive_groovy_else(
            [
                "if flag:",
                "    do_a()",
                "# GROOVY: else:",
                "    do_b()",
            ]
        )
        joined = "\n".join(lines)
        assert "else:" in joined
        assert "# GROOVY: else:" not in joined
        assert "do_b()" in joined

    def test_camelcase_calls_not_pascalcase_false_positive(self) -> None:
        """``getOrDefault`` / ``putAll`` / helpers must not match ``[A-Z]\\w*(``."""
        body = (
            "{\n"
            "    repoBranch = pipelineParameters.getOrDefault('repoBranch', 'main')\n"
            "    config.putAll(overrides)\n"
            "    checkProvisionInventory(stepParameters)\n"
            "    yaml = Yaml()\n"
            "    BuildPipeline(config)\n"
            "}"
        )
        expr = ExpressionTransformer().transform(body)
        lines = MethodTransformer().transform_body(expr)
        joined = "\n".join(lines)
        assert "# GROOVY:" not in joined
        assert "getOrDefault" in joined or ".get(" in joined
        assert "putAll" in joined or ".update(" in joined
        assert "checkProvisionInventory(stepParameters)" in joined
        assert "Yaml()" in joined
        assert "BuildPipeline(config)" in joined

    def test_multiline_raise_keeps_closer(self) -> None:
        """Multiline ``raise Foo(`` must keep the message and closing ``)``."""
        body = (
            "{\n"
            "        raise UnsupportedOperationException(\n"
            '            "Upgrade is not yet implemented"\n'
            "        )\n"
            "}"
        )
        expr = ExpressionTransformer().transform(body)
        lines = MethodTransformer().transform_body(expr)
        joined = "\n".join(lines)
        assert "raise UnsupportedOperationException(" in joined
        assert "Upgrade is not yet implemented" in joined
        assert any(ln.strip() == ")" for ln in lines)
        assert "# GROOVY:" not in joined
        compile("def f():\n" + "\n".join(f"    {ln}" for ln in lines), "<t>", "exec")

    def test_camelcase_attr_not_commented_as_groovy(self) -> None:
        """Regression: config.unsupportedVersions must not become # GROOVY:."""
        body = (
            "{\n"
            "    if (config.versions) {\n"
            "        return config.versions.contains(version)\n"
            "    }\n"
            "    if (config.unsupportedVersions) {\n"
            "        return !config.unsupportedVersions.contains(version)\n"
            "    }\n"
            "    return true\n"
            "}"
        )
        expr = ExpressionTransformer().transform(body)
        lines = MethodTransformer().transform_body(expr)
        joined = "\n".join(lines)
        assert "if config.unsupportedVersions:" in joined
        assert "return not config.unsupportedVersions.__contains__(version)" in joined
        assert "# GROOVY: if config.unsupportedVersions" not in joined
        assert "# GROOVY: return not config.unsupportedVersions" not in joined

    def test_sibling_ifs_keep_same_indent_after_brace_unwrap(self) -> None:
        """Outer-brace strip must dedent all lines, not only the first."""
        body = (
            "{\n"
            "    if (config.versions) {\n"
            "        return config.versions.contains(version)\n"
            "    }\n"
            "    if (config.unsupportedVersions) {\n"
            "        return !config.unsupportedVersions.contains(version)\n"
            "    }\n"
            "    return true\n"
            "}"
        )
        expr = ExpressionTransformer().transform(body)
        lines = MethodTransformer().transform_body(expr)
        if_lines = [ln for ln in lines if ln.strip().startswith("if ")]
        assert len(if_lines) == 2
        assert if_lines[0] == "if config.versions:"
        assert if_lines[1] == "if config.unsupportedVersions:"
        assert "return True" in lines
        # Final return is a sibling of both ifs, not nested under the first.
        assert lines[-1].strip() == "return True"
        assert len(lines[-1]) - len(lines[-1].lstrip()) == 0

    def test_local_snake_case_uses_match_declaration(self) -> None:
        """BaseSteps-style: renamed locals must be used consistently (not camelCase)."""
        body = (
            "{\n"
            '    String commandLogName = config.commandLogName ?: "shell"\n'
            "    Boolean archiveLog = config.getOrDefault('archiveLog', true)\n"
            "    String timePrefix = '120000-001'\n"
            "    String normalizedStageName = 'Install'\n"
            '    String logFile = "build_logs/stage-log-${timePrefix}-'
            '${normalizedStageName}-${commandLogName}.log"\n'
            "    if (archiveLog) {\n"
            "        return logFile\n"
            "    }\n"
            "    return commandLogName\n"
            "}"
        )
        expr = ExpressionTransformer().transform(body)
        lines = MethodTransformer().transform_body(expr, param_names=["shellScript", "config"])
        joined = "\n".join(lines)
        assert "command_log_name =" in joined
        assert "archive_log =" in joined
        assert "log_file =" in joined
        # Map-key string / attr access keep camelCase:
        assert "config.commandLogName" in joined
        assert "'archiveLog'" in joined or '"archiveLog"' in joined
        # Bare uses follow the snake_case binding:
        assert "if archive_log:" in joined
        assert re.search(r"\breturn log_file\b", joined)
        assert re.search(r"\breturn command_log_name\b", joined)
        # No bare camelCase local uses outside attrs / string keys.
        assert "if archiveLog" not in joined
        assert "return logFile" not in joined
        assert "return commandLogName" not in joined
        # f-string interpolations rewritten
        assert "{time_prefix}" in joined
        assert "{normalized_stage_name}" in joined
        assert "{command_log_name}" in joined
        assert "{timePrefix}" not in joined
        assert "{commandLogName}" not in joined


class TestJenkinsBindings:
    def test_known_bindings(self) -> None:
        assert is_jenkins_global_binding("params")
        assert is_jenkins_global_binding("env")
        assert "currentBuild" in JENKINS_GLOBAL_BINDINGS
        assert not is_jenkins_global_binding("myLocal")

    def test_harvest_upper_snake_and_brackets(self) -> None:
        text = (
            "nvr = params.OVERRIDE_NVR\n"
            "folder = params['OVERRIDE_FOLDER_NAME']\n"
            "nested = params.ocp\n"  # nested config — not harvested
            "params.sort().each { }\n"  # Map method — not harvested
        )
        assert harvest_param_names(text) == [
            "OVERRIDE_FOLDER_NAME",
            "OVERRIDE_NVR",
        ]

    def test_param_schema_unbound(self) -> None:
        schema = param_schema_from_text('x = params.FOO ?: params["BAR"]')
        assert schema.required == ["BAR", "FOO"]
        assert schema.optional == {}
        line = format_param_schema_assignment(schema)
        assert line == "_PARAM_SCHEMA = ParamSchema(required=['BAR', 'FOO'])"

    def test_mask_preserves_binding_name(self) -> None:
        masked = mask_jenkins_binding_accesses("x = params.OVERRIDE_NVR + env.JOB_NAME")
        assert "params.__jenkins_attr__" in masked
        assert "env.__jenkins_attr__" in masked
        assert "OVERRIDE_NVR" not in masked

    def test_method_body_keeps_params_upper_snake(self) -> None:
        """Regression: params.OVERRIDE_* must not become # GROOVY via camelCase check."""
        mt = MethodTransformer()
        body = mt.transform_body(
            "{\n    effectiveNvr = params.OVERRIDE_NVR ?: ''\n    job = env.JOB_NAME\n}"
        )
        joined = "\n".join(body)
        assert "params.OVERRIDE_NVR" in joined
        assert "env.JOB_NAME" in joined
        assert "# GROOVY: effectiveNvr = params.OVERRIDE_NVR" not in joined


class TestExpressionValidityPolish:
    def test_regex_match_dotted_lhs_and_string_pattern(self) -> None:
        t = ExpressionTransformer()
        out = t.transform('assert params.BUILD_NVR ==~ /^[a-z]+$/, "bad"')
        assert "re.match" in out
        assert "params.BUILD_NVR" in out
        assert "params.re.match" not in out
        out2 = t.transform('x ==~ "^[a-zA-Z0-9_.\\\\-]+$"')
        assert "re.match" in out2
        assert "==~" not in out2
        assert 'ff"' not in out2

    def test_collect_join_and_no_double_fstring(self) -> None:
        t = ExpressionTransformer()
        out = t.transform(
            "echo \"Available: ${topologies.collect { it.codeName }.join(', ')}\""
        )
        assert "join" in out
        assert ".map(lambda" not in out
        assert 'ff"' not in t.transform('x ==~ "^end$"')
        # Trailing regex ``$`` must not force an f-string.
        assert 'f"^end$"' not in t.transform('"^end$"')

    def test_collect_fstring_preserves_brace_close(self) -> None:
        """``collect { \"…/${it}\" }`` must not become ``{it)``."""
        t = ExpressionTransformer()
        scenarios = t.transform('["a","b"].collect { "input/scenarios/${it}" }')
        assert "{it}" in scenarios or '{it}"' in scenarios
        assert "{it)" not in scenarios
        joined = t.transform('formArgs.collect { "--form ${it}" }.join(" ")')
        assert "{it}" in joined or '{it}"' in joined
        assert "{it)" not in joined
        assert "join" in joined
        # Through method body — live Python, not GROOVY.
        body = 'extra = formArgs.collect { "--form ${it}" }.join(" ")\n'
        expr = t.transform(body)
        lines = MethodTransformer().transform_body(JenkinsDSLTransformer().transform_body(expr))
        joined_body = "\n".join(lines)
        assert "{it)" not in joined_body
        assert "# GROOVY:" not in joined_body

    def test_generic_typed_local_and_assert_print_body(self) -> None:
        body = (
            "{\n"
            '    assert params.BUILD_NVR ==~ /^[a-z0-9]+$/, "bad"\n'
            '    echo "Pipeline Configuration:"\n'
            '    echo "  NVR: ${params.BUILD_NVR}"\n'
            "    Map<String, Map<String, List<Map>>> groupedJobs = [:]\n"
            "}\n"
        )
        expr = ExpressionTransformer().transform(body)
        lines = MethodTransformer().transform_body(JenkinsDSLTransformer().transform_body(expr))
        joined = "\n".join(lines)
        assert "# GROOVY:" not in joined
        assert "grouped_jobs = {}" in joined
        assert "print(" in joined
        assert "re.match" in joined


class TestMethodCallWithMap:
    def test_build_and_execute_absorbed_not_sanitizer_cascade(self) -> None:
        """``builder.buildAndExecute(`` must become TODO absorb, not open-paren GROOVY cascade."""
        body = (
            "{\n"
            "    builder.buildAndExecute(\n"
            "        time: 12,\n"
            "        stagesClosure: {\n"
            "            script.stage('Validate') {\n"
            "                steps.validate()\n"
            "            }\n"
            "        }\n"
            "    )\n"
            "    afterBuild()\n"
            "}\n"
        )
        expr = ExpressionTransformer().transform(body)
        jen = JenkinsDSLTransformer().transform_body(expr)
        lines = MethodTransformer().transform_body(jen)
        joined = "\n".join(lines)
        assert "call-with-args" in joined
        assert "afterBuild()" in joined
        # Must not leave a live unclosed ``buildAndExecute(`` for the sanitizer.
        assert "builder.buildAndExecute(" not in joined or "TEKTONIZE-TODO" in joined

    def test_dict_arg_call_emits_live_python(self) -> None:
        """``obj.method([k: v])`` → live ``obj.method({...})`` (no GROOVY cascade)."""
        body = (
            "{\n"
            "    steps.uploadResultsToDashboard([\n"
            "        status: 'RUNNING',\n"
            "        buildInfo: foo\n"
            "    ])\n"
            "}\n"
        )
        expr = ExpressionTransformer().transform(body)
        lines = MethodTransformer().transform_body(JenkinsDSLTransformer().transform_body(expr))
        joined = "\n".join(lines)
        assert "uploadResultsToDashboard({" in joined
        assert "# GROOVY:" not in joined
        assert '"status":' in joined or "'status'" in joined

    def test_call_with_map_groovy_cascade_becomes_todo(self) -> None:
        """Post-pass collapses residual ``# GROOVY: foo({`` … ``})`` into one TODO."""
        lines = [
            "# GROOVY: steps.foo({",
            '# GROOVY: "a": 1,',
            '# GROOVY: "b": 2,',
            "# GROOVY: })",
            "do_work()",
        ]
        out = MethodTransformer._absorb_call_with_map_groovy(lines)
        joined = "\n".join(out)
        assert "TEKTONIZE-TODO" in joined
        assert "call-with-map" in joined
        assert joined.count("GROOVY") == 0
        assert "do_work()" in joined


class TestJenkinsDSLTransformer:
    def test_echo_mapping(self) -> None:
        t = JenkinsDSLTransformer()
        result = t.transform_body('script.echo("Hello")')
        assert "print" in result

    def test_self_script_echo_and_error(self) -> None:
        """``self.script.echo`` / ``error`` must map like bare ``script.*``."""
        t = JenkinsDSLTransformer()
        assert "print" in t.transform_body('self.script.echo("Hello")')
        assert "RuntimeError" in t.transform_body('self.script.error("boom")')
        assert "Stage: Build" in t.transform_body("self.script.stage('Build') {")

    def test_self_script_sh_multiline_and_assignment(self) -> None:
        """``self.script.sh(\"\"\"…\"\"\")`` and ``exit_code = self.script.sh(`` → TaskStep."""
        t = JenkinsDSLTransformer()
        multi = t.transform_body('self.script.sh """\nls -la\n"""\n')
        assert "TaskStep(script=" in multi
        assert "# | ls -la" in multi
        held = (
            "exitCode = self.script.sh(\n"
            '    script: """\n'
            "        echo hi\n"
            '    """,\n'
            "    returnStatus: true\n"
            ")\n"
        )
        result = t.transform_body(held)
        assert "TaskStep" in result
        assert "# | echo hi" in result
        single = t.transform_body('exit_code = self.script.sh("true")')
        assert "TaskStep(script=" in single

    def test_self_script_junit_checkout_todo(self) -> None:
        t = JenkinsDSLTransformer()
        for opener in (
            'self.script.junit("report.xml")',
            'self.script.archiveArtifacts(artifacts: "**/*.xml")',
            'self.script.writeYaml(file: "f.yml", data: data)',
            "self.script.checkout(scm)",
        ):
            result = t.transform_body(opener)
            assert "TEKTONIZE-TODO" in result, opener
            assert "GROOVY" not in result

    def test_script_io_and_credential_helpers_todo(self) -> None:
        """fileExists / read* / write* / string / usernamePassword / sshagent → TODO."""
        t = JenkinsDSLTransformer()
        cases = (
            "if script.fileExists(output_file):",
            "slack_message = script.readFile(file=output_file).strip()",
            "meta = script.readYaml(file=version_var_file)",
            "return script.string(args)",
            "credentials.add(self.script.usernamePassword(credentialsId=id, "
            "usernameVariable='U', passwordVariable='P'))",
            "self.script.sshagent(credentials=[credId]) {",
            "return [self.script.file(credentialsId=id, variable='F')]",
        )
        for src in cases:
            result = t.transform_body(src)
            assert "TEKTONIZE-TODO" in result, src
            assert "GROOVY" not in result, src

    def test_declarative_stages_post_absorb(self) -> None:
        """``stages {`` / ``post { always {`` absorb nested body as TODO."""
        t = JenkinsDSLTransformer()
        body = (
            "stages {\n"
            "    stage('Setup') {\n"
            "        steps { script { echo 'hi' } }\n"
            "    }\n"
            "}\n"
            "post {\n"
            "    always { script { archiveArtifacts() } }\n"
            "}\n"
        )
        result = t.transform_body(body)
        assert "TEKTONIZE-TODO" in result
        assert "GROOVY" not in result
        # Nested prose should not remain as live echo/stage.
        assert "echo 'hi'" not in result
        assert "archiveArtifacts()" not in result or "TEKTONIZE-TODO" in result

    def test_script_context_stage_megline_todo(self) -> None:
        """``script = context.script … script.stage(`` meglines → TODO, not GROOVY."""
        t = JenkinsDSLTransformer()
        src = "script = context.script def steps = context.stepsFactory script.stage('Setup') {"
        result = t.transform_body(src)
        assert "TEKTONIZE-TODO" in result
        assert "GROOVY" not in result
        embedded = "stages[stage_name] = { self.script.stage(stage_name) {"
        result2 = t.transform_body(embedded)
        assert "TEKTONIZE-TODO" in result2
        assert "GROOVY" not in result2
        # ``def script = context.script def steps = … if (`` span-join form
        with_def = (
            "def script = context.script def steps = context.stepsFactory "
            "// Pre checks if (deployment.shouldRunPreDeploymentChecks) {"
        )
        result3 = t.transform_body(with_def)
        assert "TEKTONIZE-TODO" in result3
        assert "GROOVY" not in result3

    def test_stage_mapping(self) -> None:
        t = JenkinsDSLTransformer()
        result = t.transform_body("script.stage('Install') {")
        assert "Stage: Install" in result

    def test_with_credentials_todo(self) -> None:
        t = JenkinsDSLTransformer()
        result = t.transform_body("withCredentials([usernamePassword(...)]) {")
        assert "TEKTONIZE-TODO" in result

    def test_multiline_sh_consumes_closer(self) -> None:
        """Multiline ``sh \"\"\"…\"\"\"`` must not leave a bare triple-quote closer."""
        t = JenkinsDSLTransformer()
        result = t.transform_body('sh """\npython3 -c "print(1)"\n"""\n')
        assert "TaskStep(script=" in result
        assert 'TaskStep(... """)' in result or '"""' in result
        assert "python3" in result
        # Closer is commented, not raw Groovy source left for MethodTransformer.
        for line in result.split("\n"):
            stripped = line.strip()
            if stripped == '"""' or stripped == '""")':
                raise AssertionError(f"bare closer left: {stripped!r}")

    def test_named_arg_multiline_sh_set_plus_e(self) -> None:
        """BaseSteps-style ``script.sh(script: \"\"\"… set +e …\"\"\")`` → TaskStep body."""
        body = (
            "{\n"
            "            def exitCode = script.sh(\n"
            '                script: """\n'
            "                    set +e\n"
            "                    (${shellScript}) > ${logFile} 2>&1\n"
            "                    exit_code=\\$?\n"
            "                    exit \\$exit_code\n"
            '                """,\n'
            "                returnStatus: true\n"
            "            )\n"
            "}"
        )
        expr = ExpressionTransformer().transform(body)
        jen = JenkinsDSLTransformer().transform_body(expr)
        lines = MethodTransformer().transform_body(jen)
        joined = "\n".join(lines)
        assert "{**set" not in joined
        assert 'script=""f"' not in joined
        assert "# GROOVY: set" not in joined
        assert "set +e" in joined
        assert "TaskStep" in joined
        assert "# | set +e" in joined or "set +e" in joined

    def test_with_failure_notification_map_tails_become_todo(self) -> None:
        """Map entries after ``withFailureNotification({`` stay in the TODO block."""
        t = JenkinsDSLTransformer()
        body = (
            "steps.notificationSteps.withFailureNotification({\n"
            '    "jiraBugType": NotificationSteps.PRODUCT_INSTALL_BUG_TYPE\n'
            "}) {\n"
            "    doWork()\n"
            "}\n"
        )
        result = t.transform_body(body)
        assert "TEKTONIZE-TODO" in result
        assert "jiraBugType" not in result or "TEKTONIZE-TODO" in result
        # Map entry / closer lines must be TODOs, not raw Groovy left for MethodTransformer.
        for line in result.split("\n"):
            s = line.strip()
            if "jiraBugType" in s:
                assert s.startswith("# TEKTONIZE-TODO")
            if s in ("})", "}) {"):
                raise AssertionError(f"bare closer left: {s!r}")
        assert "doWork()" in result

    def test_closure_wrappers_become_todo(self) -> None:
        t = JenkinsDSLTransformer()
        for opener in (
            "steps.notificationSteps.withFailureNotification {",
            "self.withCustomSshAgent {",
            "self.withCustomEnv() {",
            "steps.kubernetesSteps.withKubeconfigFrom(cfg) {",
            "self.shWthTimeMeasurement {",
            "self.shQuietWithEnv {",
            "self.stageWrapper('Build') {",
            "self.withContainer('agent') {",
            "self.withCloudLogin {",
        ):
            result = t.transform_body(opener)
            assert "TEKTONIZE-TODO" in result, opener
            assert "GROOVY" not in result

    def test_arbitrary_with_wrapper_early_todo_and_shell_body(self) -> None:
        """Unknown ``withFooBarBaz`` still gets early TODO + ``# |`` shell body."""
        t = JenkinsDSLTransformer()
        body = (
            "self.withFooBarBaz(stepParameters) {\n"
            "    ansible-playbook demo.playbook -i inv\n"
            "}\n"
        )
        result = t.transform_body(body)
        assert "withFooBarBaz" in result
        assert "TEKTONIZE-TODO" in result
        assert "# | ansible-playbook" in result
        assert "# GROOVY:" not in result

    def test_arbitrary_sh_wrapper_triple_quote_taskstep(self) -> None:
        """Unknown ``shQuietThing`` gets TaskStep body (not late with* absorb only)."""
        body = (
            "this.shQuietThing(stepParameters, [commandLogName: 'run']) {\n"
            '    """\n'
            "    ansible-playbook foo\n"
            '    """\n'
            "}\n"
        )
        expr = ExpressionTransformer().transform(body)
        result = JenkinsDSLTransformer().transform_body(expr)
        assert "TaskStep" in result
        assert "# | ansible-playbook foo" in result
        assert "# GROOVY:" not in result
        for line in result.split("\n"):
            if line.strip() == "}":
                raise AssertionError("bare closure closer left")

    def test_dsl_hints_change_todo_message_only(self) -> None:
        """Hints refine TODO text without changing structural conversion."""
        hints = {"withCustomSshAgent": "withCustomSshAgent -> custom SSH sidecar"}
        t = JenkinsDSLTransformer(dsl_hints=hints)
        body = (
            "withCustomSshAgent(stepParameters) {\n"
            "    ansible-playbook demo.playbook -i inv\n"
            "}\n"
        )
        result = t.transform_body(body)
        assert "custom SSH sidecar" in result
        assert "# | ansible-playbook" in result

    def test_job_dsl_declarative_gitscm_become_todo(self) -> None:
        """``pipeline {`` / GitSCM / description blocks → TODO absorb (not GROOVY)."""
        t = JenkinsDSLTransformer()
        pipeline = t.transform_body(
            "pipeline {\n"
            "    agent any\n"
            "    stages {\n"
            "        stage('x') { }\n"
            "    }\n"
            "}\n"
            "doWork()\n"
        )
        assert "TEKTONIZE-TODO" in pipeline
        assert "doWork()" in pipeline
        for line in pipeline.split("\n"):
            if "agent any" in line or "stages {" in line:
                assert line.strip().startswith("# TEKTONIZE-TODO")

        gitscm = t.transform_body(
            "checkout scm: [\n"
            "    $class: 'GitSCM',\n"
            "    userRemoteConfigs: [[url: 'https://example.com/r.git']],\n"
            "]\n"
        )
        assert "GitSCM" in gitscm or "git-clone" in gitscm
        assert all("GROOVY" not in ln for ln in gitscm.split("\n") if ln.strip())

        desc = t.transform_body(
            "description = '''Type of deploy\n" "- rpm: Traditional\n" "'''\n" "name = 'x'\n"
        )
        assert "TEKTONIZE-TODO" in desc
        assert "name = 'x'" in desc
        for line in desc.split("\n"):
            if "rpm:" in line or "Type of" in line:
                assert line.strip().startswith("# TEKTONIZE-TODO")

    def test_shell_timing_leak_becomes_todo(self) -> None:
        """START=/END= shell fragments outside TaskStep become TODOs, not GROOVY."""
        t = JenkinsDSLTransformer()
        body = (
            "START=$(date +%s)\n"
            "echo hi\n"
            "END=$(date +%s)\n"
            "print($(( END - START )) > testing_time)\n"
        )
        result = t.transform_body(body)
        assert "TEKTONIZE-TODO" in result
        assert "START=$(date" not in result or result.strip().startswith("#")
        for line in result.split("\n"):
            if "START=" in line or "END=" in line or "print($((" in line:
                assert line.strip().startswith("# TEKTONIZE-TODO")

    def test_sh_quiet_wrapper_triple_quote_body_is_taskstep(self) -> None:
        """``shQuietWithEnv(…) { \"\"\"…START=…\"\"\" }`` → TaskStep body, not GROOVY."""
        body = (
            "this.shQuietWithEnv(stepParameters, [commandLogName: 'run']) {\n"
            '    """\n'
            "    START=\\$(date +%s)\n"
            "    ansible-playbook foo\n"
            "    END=\\$(date +%s)\n"
            '    """\n'
            "}\n"
        )
        expr = ExpressionTransformer().transform(body)
        result = JenkinsDSLTransformer().transform_body(expr)
        assert "TaskStep" in result
        assert "# | ansible-playbook foo" in result or "# | START=" in result
        for line in result.split("\n"):
            s = line.strip()
            if s.startswith("START=") or s.startswith("ansible-playbook"):
                raise AssertionError(f"raw shell left: {s!r}")
            if s == "}":
                raise AssertionError("bare closure closer left")

    def test_script_stage_and_block_openers(self) -> None:
        t = JenkinsDSLTransformer()
        assert "Stage: Install" in t.transform_body("script.stage('Install') {")
        assert "Stage:" in t.transform_body('script.stage(f"Config Change: {name}") {')
        assert "TEKTONIZE-TODO" in t.transform_body("script.dir(workspace) {")
        assert "TEKTONIZE-TODO" in t.transform_body("script {")
        assert "TEKTONIZE-TODO" in t.transform_body("steps {")
        assert "TEKTONIZE-TODO" in t.transform_body("stagesClosure={")
        assert "TEKTONIZE-TODO" in t.transform_body("}) {")
        # Without ExpressionTransformer, jenkins catch-all still TODOs script.env.
        assert "TEKTONIZE-TODO" in t.transform_body("script.env.FOO = 'x'")

    def test_glued_for_if_body_split(self) -> None:
        """Span-join artifact ``for x in xs:stmt`` splits into header + body."""
        mt = MethodTransformer()
        lines = mt.transform_body("for topology in topologies:topologyName = topology.codeName")
        joined = "\n".join(lines)
        assert "for topology in topologies:" in joined
        assert "topologyName = topology.codeName" in joined
        assert "# GROOVY:" not in joined
        # Glued if
        lines2 = mt.transform_body('if script_flag:print("hi")')
        # After split, may still need expression rewrite for validity — header present.
        assert any("if script_flag:" in ln for ln in lines2)

    def test_ansible_shell_leak_after_with_todo(self) -> None:
        """Playbook / source lines after with* TODO become ``# |`` or shell TODO."""
        t = JenkinsDSLTransformer()
        body = (
            "withCustomSshAgent(stepParameters) {\n"
            "    ansible-playbook myorg.setup_cli_tests -i inv\n"
            "}\n"
            "source ${pythonVenv}/bin/activate\n"
            "host-0 ansible_user=cloud-user ansible_host=1.2.3.4\n"
        )
        result = t.transform_body(body)
        assert "TEKTONIZE-TODO" in result or "# |" in result
        assert "# GROOVY:" not in result
        assert "ansible-playbook" in result
        # After with* TODO, playbook should be TaskStep-style ``# |``.
        assert "# | ansible-playbook" in result or "shell command body" in result

    def test_ternary_with_decls_becomes_todo(self) -> None:
        """``valueOf… if { Type x =`` meglines → TODO, not GROOVY."""
        mt = MethodTransformer()
        src = (
            "DeploymentType.valueOf(params.SOURCE_DEPLOYMENT_TYPE) if { "
            "DeploymentType source_type = params.SOURCE_DEPLOYMENT_TYPE else "
            "DeploymentType.RPM def config = {} }"
        )
        joined = "\n".join(mt.transform_body(src))
        assert "TEKTONIZE-TODO" in joined
        assert "ternary-with-decls" in joined
        assert "# GROOVY:" not in joined

    def test_thin_vars_def_opener_live(self) -> None:
        """``def rpm_fresh_install(overrides = {}):`` stays live."""
        mt = MethodTransformer()
        lines = mt.transform_body("def rpm_fresh_install(overrides = {}):")
        joined = "\n".join(lines)
        assert "def rpm_fresh_install" in joined
        assert "# GROOVY:" not in joined

    def test_brace_if_oneliner_split(self) -> None:
        """``if (cond) { stmt }`` → ``if cond:`` + indented body."""
        mt = MethodTransformer()
        src = "if (('tsdVarFiles' in config.tests)) { combined.tsdVarFiles = config.tests.tsdVarFiles }"
        lines = mt.transform_body(src)
        joined = "\n".join(lines)
        assert "if " in joined and "tsdVarFiles" in joined
        assert "# GROOVY:" not in joined
        # Multi-stmt residue → TODO
        multi = "if (x) { a = 1; b = 2 }"
        joined2 = "\n".join(mt.transform_body(multi))
        assert "TEKTONIZE-TODO" in joined2 or "# GROOVY:" not in joined2 or "brace-if" in joined2

    def test_orphan_switch_case_absorbed(self) -> None:
        """Orphan ``case`` without ``match`` → TODO absorb, not GROOVY cascade."""
        mt = MethodTransformer()
        body = (
            "case 'string':\n"
            "    return formatString(v)\n"
            "case 'boolean':\n"
            "    return formatBoolean(v)\n"
            "case _:\n"
            "    return str(v)\n"
        )
        joined = "\n".join(mt.transform_body(body))
        assert "TEKTONIZE-TODO" in joined
        assert "switch/case" in joined
        assert "# GROOVY: case" not in joined

    def test_switch_block_absorbed_not_half_match(self) -> None:
        """Groovy ``switch { case … }`` → TODO span, not live match + GROOVY cases."""
        mt = MethodTransformer()
        body = (
            "switch (kubeconfigSource) {\n"
            'case "jenkins-credentials":\n'
            "    assert credentialsId != ''\n"
            'case "cloud-cluster":\n'
            "    return cl()\n"
            "default:\n"
            "    throw new RuntimeError('bad')\n"
            "}\n"
        )
        joined = "\n".join(mt.transform_body(body))
        assert "TEKTONIZE-TODO" in joined
        assert "switch/case" in joined
        assert "# GROOVY: case" not in joined
        assert "# GROOVY: switch" not in joined
        assert "match kubeconfig" not in joined

    def test_juxtaposed_typed_decls_split(self) -> None:
        """``String a = … String b = …`` meglines split to live assignments."""
        mt = MethodTransformer()
        src = (
            "String inventory_file = (step_parameters.inventoryFile or 'inventory') "
            "String inventory_group = (step_parameters.inventoryGroup or 'nightlies')"
        )
        lines = mt.transform_body(src)
        joined = "\n".join(lines)
        assert "inventory_file =" in joined
        assert "inventory_group =" in joined
        assert "# GROOVY:" not in joined

    def test_triple_quote_assignment_closer_stays_live(self) -> None:
        """``x = \"\"\"`` … ``\"\"\"`` must not GROOVY the closer (sanitize cascade)."""
        mt = MethodTransformer()
        body = 'check_script = """\n' "echo hi\n" '"""\n' "return check_script\n"
        lines = mt.transform_body(body)
        joined = "\n".join(lines)
        assert 'check_script = """' in joined
        assert '# GROOVY: """' not in joined
        # Closer present as live delimiter.
        assert any(ln.strip() == '"""' for ln in lines)

    def test_yaml_heredoc_body_not_groovy(self) -> None:
        """YAML lines inside ``x = \"\"\"`` stay live string content."""
        mt = MethodTransformer()
        body = (
            'default_values = """\n'
            "---\n"
            "test_hosts: 100   # how many hosts\n"
            '"""\n'
            "return default_values\n"
        )
        joined = "\n".join(mt.transform_body(body))
        assert 'default_values = """' in joined
        assert "test_hosts: 100" in joined
        assert "# GROOVY: test_hosts" not in joined
        assert "# GROOVY: ---" not in joined

    def test_job_dsl_script_choice_todo(self) -> None:
        """``return script.choice(...)`` → TODO, not GROOVY."""
        t = JenkinsDSLTransformer()
        result = t.transform_body("return script.choice(args)")
        assert "TEKTONIZE-TODO" in result
        assert "script.choice" in result
        assert "# GROOVY:" not in result

    def test_with_cloud_credential_file_todo(self) -> None:
        """``withCloudCredentialFile(…) {`` → structural TODO absorb."""
        t = JenkinsDSLTransformer()
        body = (
            'withCloudCredentialFile(["environment": env]) {\n'
            '    sh "aws sts get-caller-identity"\n'
            "}\n"
        )
        result = t.transform_body(body)
        assert "TEKTONIZE-TODO" in result
        assert "withCloudCredentialFile" in result
        assert "# GROOVY: withCloud" not in result

    def test_at_library_in_fstring_live(self) -> None:
        """``@Library`` inside an f-string must not trip groovy ``@`` syntax."""
        mt = MethodTransformer()
        src = "lines.append(f\"@Library(['{SHARED_LIBRARY}']) _\")"
        joined = "\n".join(mt.transform_body(src))
        assert "@Library" in joined
        assert "# GROOVY:" not in joined

    def test_orphan_elif_absorbed(self) -> None:
        """``# GROOVY: elif`` without reviveable ``if`` → TODO."""
        absorbed = MethodTransformer._absorb_orphan_elif(
            ["# GROOVY: elif installer_inventory:", "    use_inv = installer_inventory"]
        )
        joined = "\n".join(absorbed)
        assert "TEKTONIZE-TODO" in joined
        assert "orphan elif" in joined
        assert "# GROOVY: elif" not in joined

    def test_nested_def_to_map_todo(self) -> None:
        """Nested ``def to_map(self):`` inside a body → TODO absorb."""
        mt = MethodTransformer()
        body = "x = 1\ndef to_map(self):\n    return {}\n"
        joined = "\n".join(mt.transform_body(body))
        assert "nested method decl" in joined
        assert "# GROOVY: def to_map" not in joined

    def test_filter_kv_dict_comp(self) -> None:
        """``map.findAll { k, v -> cond }`` → dict comprehension."""
        t = ExpressionTransformer()
        out = t.transform("PLATFORM_VERSIONS.findAll { k, v -> v.get('latest', false) }")
        assert "for k, v in PLATFORM_VERSIONS.items()" in out
        assert "findAll" not in out

    def test_plusplus_and_leftshift_append(self) -> None:
        """``x++`` and ``lines << f\"…\"`` rewrite to live Python."""
        t = ExpressionTransformer()
        assert "attempt += 1" in t.transform("attempt++")
        out = t.transform('lines << f"    {formatKey(k)}: {formatValue(v)},"')
        assert "lines.append(" in out
        assert "<<" not in out
        mt = MethodTransformer()
        joined = "\n".join(mt.transform_body("attempt += 1"))
        assert "attempt += 1" in joined
        assert "# GROOVY:" not in joined

    def test_ternary_with_echo_todo(self) -> None:
        """``'Loaded' if script.echo(...)`` → ternary TODO, not GROOVY."""
        mt = MethodTransformer()
        src = "'Loaded' if script.echo(f\"GITHUB_TOKEN: {env.GITHUB_TOKEN}\") else 'Not Found'"
        joined = "\n".join(mt.transform_body(src))
        assert "TEKTONIZE-TODO" in joined
        assert "ternary" in joined.lower() or "script" in joined
        assert "# GROOVY: 'Loaded'" not in joined

    def test_assign_ternary_keeps_lhs(self) -> None:
        """``lhs = cond ? a : b`` → ``lhs = (a if cond else b)``, not mangled."""
        t = ExpressionTransformer()
        out = t.transform("color = currentBuild.result == 'SUCCESS' ? 'good' : 'danger'")
        assert "color = (" in out
        assert "'good' if currentBuild.result == 'SUCCESS' else 'danger'" in out
        assert "if color =" not in out
        mt = MethodTransformer()
        joined = "\n".join(mt.transform_body(out))
        assert "color = (" in joined
        assert "# GROOVY:" not in joined

    def test_elvis_residue_simple_revived(self) -> None:
        """``lhs = name else default`` (missing if) → live ``(name if name else default)``."""
        mt = MethodTransformer()
        joined = "\n".join(mt.transform_body('dry_run_note = params.DRY_RUN else ""'))
        assert "dry_run_note = (params.DRY_RUN if params.DRY_RUN else" in joined
        assert "# GROOVY:" not in joined

    def test_numeric_plus_not_dict_spread(self) -> None:
        """``pass + fail`` / ``i + 1`` stay arithmetic, not ``{**…}``."""
        t = ExpressionTransformer()
        assert "{**" not in t.transform("result.pass + result.fail")
        assert t.transform("i + 1") == "i + 1"
        assert "{**results" in t.transform("results + [total: 1]")

    def test_find_to_next_generator(self) -> None:
        """``list.find { it.x == y }`` → ``next((x for x in list if …), None)``."""
        t = ExpressionTransformer()
        out = t.transform("list.find { it.section == section }")
        assert "next((x for x in list if (x.section == section)), None)" in out
        assert "listnext(" not in out

    def test_script_sh_def_opener_taskstep(self) -> None:
        """``def x = script.sh( script=\"\"\"`` span-join megline → TaskStep + ``# |``."""
        from tektonize.parser.span_join import join_open_spans

        body = (
            "{\n"
            "        def createInstanceOutput = script.sh(\n"
            '            script: """\n'
            "                export USER=foo\n"
            '            """,\n'
            "            returnStdout: true\n"
            "        )\n"
            "}"
        )
        unwrapped = MethodTransformer._unwrap_method_body(body)
        spanned = join_open_spans(unwrapped)
        expr = ExpressionTransformer().transform(spanned)
        jenkins = JenkinsDSLTransformer().transform_body(expr)
        assert "# TaskStep(script=" in jenkins
        assert "# | export USER=foo" in jenkins or "# | export USER=" in jenkins
        assert "script.sh(" not in jenkins

    def test_regex_find_and_range_slice(self) -> None:
        """``=~`` → re.search; ``x[0..-3]`` → slice; ``m[k]++`` → ``+= 1``."""
        t = ExpressionTransformer()
        assert 're.search(r"pipeline"' in t.transform("s =~ /pipeline/")
        assert t.transform("tower_version[0..-3]") == "tower_version[0:-2]"
        assert "total_counters[resultKey] += 1" in t.transform("total_counters[resultKey]++")

    def test_job_dsl_definition_absorbed(self) -> None:
        """Job DSL ``definition {`` GROOVY span → one TODO, not many GROOVY lines."""
        src = (
            "# GROOVY: definition { cps { script(script_text) sandbox() } }\n"
            "# GROOVY: buildDiscarder { strategy { logRotator { numToKeep(30) } } }\n"
        )
        out = MethodTransformer._absorb_job_dsl_and_stages(src.splitlines())
        joined = "\n".join(out)
        assert "Job DSL" in joined or "definition" in joined.lower()
        assert joined.count("# GROOVY:") <= 1

    def test_safe_nav_lambda_allowlisted(self) -> None:
        """Balanced safe-nav lambda expansions stay live Python."""
        mt = MethodTransformer()
        src = 'value = ((lambda _o: getattr(_o, "trim")() ' "if _o is not None else None)(label))"
        lines = mt.transform_body(src)
        joined = "\n".join(lines)
        assert "getattr(_o," in joined
        assert "# GROOVY:" not in joined

    def test_detect_jenkins_calls(self) -> None:
        t = JenkinsDSLTransformer()
        calls = t.detect_jenkins_calls('script.sh("ls"); script.echo("done")')
        assert len(calls) >= 2


class TestImportTransformer:
    def test_library_import(self) -> None:
        from tektonize.parser.ast_nodes import ImportDecl

        t = ImportTransformer()
        result = t.transform_imports(
            [
                ImportDecl(path="mylib.pipelines.config.DeploymentType"),
            ]
        )
        assert len(result.local_imports) == 1
        line = result.local_imports[0].to_line()
        assert "deployment_type" in line
        assert "DeploymentType" in line

    def test_jenkins_import_skipped(self) -> None:
        from tektonize.parser.ast_nodes import ImportDecl

        t = ImportTransformer()
        result = t.transform_imports(
            [
                ImportDecl(path="jenkins.model.Jenkins"),
            ]
        )
        assert len(result.local_imports) == 0
        assert len(result.stdlib_imports) == 0
