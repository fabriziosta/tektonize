"""Tests for the Python emitter."""

from __future__ import annotations

import py_compile
import tempfile
from pathlib import Path

import pytest

from tektonize.emitter.python_emitter import PythonEmitter
from tektonize.parser.groovy_ast import GroovyParser

SAMPLE_LIB = Path(__file__).parent / "fixtures" / "sample_lib"


@pytest.fixture
def parser() -> GroovyParser:
    return GroovyParser()


@pytest.fixture
def emitter() -> PythonEmitter:
    return PythonEmitter()


def _check_valid_python(source: str) -> bool:
    """Check if the generated source is valid Python."""
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(source)
        f.flush()
        try:
            py_compile.compile(f.name, doraise=True)
            return True
        except py_compile.PyCompileError:
            return False


class TestEnumEmission:
    def test_deployment_type(self, parser: GroovyParser, emitter: PythonEmitter) -> None:
        parsed = parser.parse_file(SAMPLE_LIB / "src/mylib/config/DeploymentType.groovy")
        output = emitter.emit_file(parsed)

        assert "class DeploymentType(Enum):" in output
        assert "KUBERNETES = auto()" in output
        assert "CONTAINERIZED = auto()" in output
        assert "RPM = auto()" in output
        assert "AZURE = auto()" in output
        assert "SAAS = auto()" in output
        assert "from enum import Enum, auto" in output
        assert _check_valid_python(output)


class TestDataclassEmission:
    def test_installer_settings_structure(
        self, parser: GroovyParser, emitter: PythonEmitter
    ) -> None:
        parsed = parser.parse_file(SAMPLE_LIB / "src/mylib/config/InstallerSettings.groovy")
        output = emitter.emit_file(parsed)

        assert "@dataclass" in output
        assert "class InstallerSettings" in output
        assert "installer_type: str" in output
        assert "list[str]" in output
        assert "field(default_factory=list)" in output
        assert "from dataclasses import dataclass, field" in output


class TestInterfaceEmission:
    def test_deployment_strategy(self, parser: GroovyParser, emitter: PythonEmitter) -> None:
        parsed = parser.parse_file(SAMPLE_LIB / "src/mylib/deployments/DeploymentStrategy.groovy")
        output = emitter.emit_file(parsed)

        assert "class DeploymentStrategy(ABC):" in output
        assert "from abc import ABC, abstractmethod" in output
        assert "@abstractmethod" in output


class TestVarsEmission:
    def test_vars_produces_function(self, parser: GroovyParser, emitter: PythonEmitter) -> None:
        parsed = parser.parse_file(SAMPLE_LIB / "vars/ExamplePipeline.groovy")
        output = emitter.emit_file(parsed)

        assert "def " in output
