"""Tests for Groovy source discovery and test-file exclusion."""

from __future__ import annotations

from pathlib import Path

from tektonize.converter import Converter
from tektonize.discovery import (
    find_shared_library_root,
    is_test_groovy,
    iter_groovy_sources,
)


def test_filename_suffixes_recognized_as_tests(tmp_path: Path) -> None:
    root = tmp_path
    assert is_test_groovy(root / "FooTest.groovy", root=root)
    assert is_test_groovy(root / "FooTests.groovy", root=root)
    assert is_test_groovy(root / "FooSpec.groovy", root=root)
    assert is_test_groovy(root / "FooTestCase.groovy", root=root)


def test_config_named_test_scenario_is_not_a_test(tmp_path: Path) -> None:
    """``TestScenario.groovy`` is a type name, not a JUnit/Spock test class."""
    root = tmp_path
    assert not is_test_groovy(root / "TestScenario.groovy", root=root)
    assert not is_test_groovy(root / "src" / "mylib" / "config" / "TestScenario.groovy", root=root)


def test_under_test_directory_excluded(tmp_path: Path) -> None:
    root = tmp_path
    assert is_test_groovy(root / "test" / "Helper.groovy", root=root)
    assert is_test_groovy(root / "tests" / "unit" / "Helper.groovy", root=root)
    assert is_test_groovy(root / "src" / "test" / "groovy" / "Helper.groovy", root=root)
    assert is_test_groovy(root / "TEST" / "Helper.groovy", root=root)


def test_parent_named_tests_does_not_exclude_library(tmp_path: Path) -> None:
    """Library root under a folder named ``tests`` must still convert."""
    lib = tmp_path / "tests" / "fixtures" / "sample_lib"
    (lib / "src" / "mylib").mkdir(parents=True)
    src = lib / "src" / "mylib" / "Steps.groovy"
    src.write_text("class Steps {}\n", encoding="utf-8")
    assert not is_test_groovy(src, root=lib)


def test_iter_groovy_sources_skips_tests(tmp_path: Path) -> None:
    (tmp_path / "vars").mkdir()
    (tmp_path / "vars" / "Pipeline.groovy").write_text("def call() {}\n", encoding="utf-8")
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "PipelineSpec.groovy").write_text(
        "class PipelineSpec {}\n", encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "UtilTest.groovy").write_text("class UtilTest {}\n", encoding="utf-8")
    (tmp_path / "src" / "Util.groovy").write_text("class Util {}\n", encoding="utf-8")

    sources, skipped = iter_groovy_sources(tmp_path)
    names = {p.name for p in sources}
    assert names == {"Pipeline.groovy", "Util.groovy"}
    assert len(skipped) == 2
    assert any("PipelineSpec" in s for s in skipped)
    assert any("UtilTest" in s for s in skipped)


def test_find_shared_library_root(tmp_path: Path) -> None:
    lib = tmp_path / "my-lib"
    nested = lib / "src" / "mylib" / "config"
    nested.mkdir(parents=True)
    (lib / "vars").mkdir()
    groovy = nested / "DeploymentType.groovy"
    groovy.write_text("enum DeploymentType { RPM }\n", encoding="utf-8")
    assert find_shared_library_root(groovy) == lib.resolve()


def test_converter_skips_test_files(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    (lib / "vars").mkdir(parents=True)
    (lib / "vars" / "Pipeline.groovy").write_text(
        "def call() { echo 'hi' }\n",
        encoding="utf-8",
    )
    (lib / "test").mkdir()
    (lib / "test" / "PipelineSpec.groovy").write_text(
        "class PipelineSpec {}\n",
        encoding="utf-8",
    )

    out = tmp_path / "out"
    report = Converter(input_dir=lib, output_dir=out, stage_filter={"05_final"}).run()
    assert report["total_files"] == 1
    assert report["skipped_test_files"] == 1
    assert report["succeeded"] == 1
