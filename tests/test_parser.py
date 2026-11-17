"""Tests for the Groovy AST parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from tektonize.parser.ast_nodes import NodeKind
from tektonize.parser.groovy_ast import GroovyParser

SAMPLE_LIB = Path(__file__).parent / "fixtures" / "sample_lib"


@pytest.fixture
def parser() -> GroovyParser:
    return GroovyParser()


class TestEnumParsing:
    def test_deployment_type_enum(self, parser: GroovyParser) -> None:
        result = parser.parse_file(SAMPLE_LIB / "src/mylib/config/DeploymentType.groovy")
        assert result.package == "mylib.config"
        assert len(result.classes) == 1

        enum = result.classes[0]
        assert enum.kind == NodeKind.ENUM
        assert enum.name == "DeploymentType"
        assert len(enum.values) == 5
        assert [v.name for v in enum.values] == [
            "KUBERNETES",
            "CONTAINERIZED",
            "RPM",
            "AZURE",
            "SAAS",
        ]

    def test_test_scenario_enum(self, parser: GroovyParser) -> None:
        result = parser.parse_file(SAMPLE_LIB / "src/mylib/config/TestScenario.groovy")
        assert len(result.classes) == 1
        assert result.classes[0].kind == NodeKind.ENUM
        assert result.classes[0].name == "TestScenario"
        assert len(result.classes[0].values) >= 3


class TestClassParsing:
    def test_installer_settings(self, parser: GroovyParser) -> None:
        result = parser.parse_file(SAMPLE_LIB / "src/mylib/config/InstallerSettings.groovy")
        assert len(result.classes) == 1
        cls = result.classes[0]
        assert cls.name == "InstallerSettings"
        assert len(cls.fields) >= 10
        field_names = [f.name for f in cls.fields]
        assert "installerType" in field_names or "installer_type" in field_names

    def test_base_steps_abstract(self, parser: GroovyParser) -> None:
        result = parser.parse_file(SAMPLE_LIB / "src/mylib/steps/BaseSteps.groovy")
        assert len(result.classes) >= 1
        cls = result.classes[0]
        assert cls.name == "BaseSteps"
        assert "abstract" in cls.modifiers or len(cls.methods) > 0


class TestInterfaceParsing:
    def test_deployment_strategy(self, parser: GroovyParser) -> None:
        result = parser.parse_file(SAMPLE_LIB / "src/mylib/deployments/DeploymentStrategy.groovy")
        assert len(result.classes) == 1
        iface = result.classes[0]
        assert iface.name == "DeploymentStrategy"
        assert iface.kind == NodeKind.INTERFACE
        assert len(iface.methods) >= 5


class TestVarsFileParsing:
    def test_example_pipeline(self, parser: GroovyParser) -> None:
        result = parser.parse_file(SAMPLE_LIB / "vars/ExamplePipeline.groovy")
        assert len(result.top_level_methods) >= 1
        assert any(m.name == "call" for m in result.top_level_methods)


class TestImportParsing:
    def test_imports_extracted(self, parser: GroovyParser) -> None:
        result = parser.parse_file(SAMPLE_LIB / "src/mylib/scenarios/FreshInstallScenario.groovy")
        assert len(result.imports) >= 1
        import_paths = [i.path for i in result.imports]
        assert any("DeploymentStrategy" in p for p in import_paths)


class TestRawTree:
    def test_raw_tree_structure(self, parser: GroovyParser) -> None:
        source = "package mylib.test\n\nenum Color { RED, GREEN, BLUE }\n"
        tree = parser.get_raw_tree(source)
        assert tree["type"] in ("source_file", "program")
        assert "children" in tree


class TestFallbackMethodExtraction:
    def test_get_or_default_calls_are_not_methods(self) -> None:
        from tektonize.parser.fallback_parser import parse_groovy_fallback

        source = """
package mylib.config
class Settings {
    static Settings fromMap(Map params) {
        def settings = new Settings()
        settings.x = params.getOrDefault('x', false)
        return settings
    }
    Map toMap() {
        return [x: x]
    }
}
"""
        result = parse_groovy_fallback(source, "Settings.groovy")
        assert len(result.classes) == 1
        names = [m.name for m in result.classes[0].methods]
        assert names == ["fromMap", "toMap"]

    def test_fragmented_class_uses_fallback(self, parser: GroovyParser) -> None:
        """When tree-sitter finds orphan methods but no class, prefer fallback."""
        source = (SAMPLE_LIB / "src/mylib/scenarios/FreshInstallScenario.groovy").read_text()
        result = parser.parse_source(source, "FreshInstallScenario.groovy")
        assert any(c.name == "FreshInstallScenario" for c in result.classes)
