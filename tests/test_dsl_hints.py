"""Tests for optional DSL hint overlays."""

from __future__ import annotations

from pathlib import Path

import pytest

from tektonize.dsl_hints import load_dsl_hints
from tektonize.tekton.task_definition import DEFAULT_TASK_IMAGE, TaskStep
from tektonize.transformer.jenkins_dsl_transformer import JenkinsDSLTransformer


def test_load_dsl_hints_yaml(tmp_path: Path) -> None:
    path = tmp_path / "hints.yaml"
    path.write_text("withFoo: \"withFoo -> custom message\"\n", encoding="utf-8")
    assert load_dsl_hints(path) == {"withFoo": "withFoo -> custom message"}


def test_load_dsl_hints_json(tmp_path: Path) -> None:
    path = tmp_path / "hints.json"
    path.write_text('{"shQuietThing": "shQuietThing -> quiet shell"}', encoding="utf-8")
    assert load_dsl_hints(path) == {"shQuietThing": "shQuietThing -> quiet shell"}


def test_load_dsl_hints_none() -> None:
    assert load_dsl_hints(None) == {}


def test_load_dsl_hints_rejects_bad_shape(tmp_path: Path) -> None:
    path = tmp_path / "hints.yaml"
    path.write_text("- not a map\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_dsl_hints(path)


def test_default_task_image_override(monkeypatch: pytest.MonkeyPatch) -> None:
    import tektonize.tekton.task_definition as td

    monkeypatch.setattr(td, "DEFAULT_TASK_IMAGE", "busybox:latest")
    assert TaskStep().image == "busybox:latest"
    monkeypatch.setattr(td, "DEFAULT_TASK_IMAGE", DEFAULT_TASK_IMAGE)
    assert TaskStep().image == DEFAULT_TASK_IMAGE


def test_hints_do_not_affect_unlisted_wrappers() -> None:
    t = JenkinsDSLTransformer(dsl_hints={"withOther": "custom"})
    out = t.transform_body("withUnlisted {\n    ansible-playbook x\n}\n")
    assert "withUnlisted -> Tekton wrapper" in out
    assert "custom" not in out
