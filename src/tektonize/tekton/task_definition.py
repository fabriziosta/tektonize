"""Tekton TaskDefinition and TaskStep dataclasses.

These are the target types that converted step classes will return.
A TaskDefinition represents a single reusable Tekton Task (one or more steps).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Overridable via ``Converter(default_image=…)`` / ``--default-image``.
DEFAULT_TASK_IMAGE = "ubuntu:22.04"


def _default_task_image() -> str:
    return DEFAULT_TASK_IMAGE


@dataclass
class TaskStep:
    """A single step within a Tekton Task."""

    name: str = ""
    image: str = field(default_factory=_default_task_image)
    script: str = ""
    command: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    env_from_secrets: list[str] = field(default_factory=list)
    working_dir: str = "$(workspaces.shared-workspace.path)"
    on_error: str = ""  # "continue" or empty
    timeout: str = ""
    resources: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"name": self.name}
        if self.image:
            result["image"] = self.image
        if self.script:
            result["script"] = self.script
        if self.command:
            result["command"] = self.command
        if self.args:
            result["args"] = self.args
        if self.env:
            result["env"] = [{"name": k, "value": v} for k, v in self.env.items()]
        if self.env_from_secrets:
            result["envFrom"] = self.env_from_secrets
        if self.working_dir:
            result["workingDir"] = self.working_dir
        if self.on_error:
            result["onError"] = self.on_error
        if self.timeout:
            result["timeout"] = self.timeout
        return result


@dataclass
class TaskDefinition:
    """A complete Tekton Task definition."""

    name: str = ""
    description: str = ""
    steps: list[TaskStep] = field(default_factory=list)
    params: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, str]] = field(default_factory=list)
    workspaces: list[dict[str, str]] = field(default_factory=list)
    volumes: list[dict[str, Any]] = field(default_factory=list)
    step_template: dict[str, Any] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    source_class: str = ""
    source_method: str = ""

    def to_dict(self) -> dict[str, Any]:
        spec: dict[str, Any] = {}
        if self.description:
            spec["description"] = self.description
        if self.params:
            spec["params"] = self.params
        if self.results:
            spec["results"] = self.results
        if self.workspaces:
            spec["workspaces"] = self.workspaces
        if self.step_template:
            spec["stepTemplate"] = self.step_template
        spec["steps"] = [s.to_dict() for s in self.steps]

        return {
            "apiVersion": "tekton.dev/v1",
            "kind": "Task",
            "metadata": {
                "name": self.name,
                **({"labels": self.labels} if self.labels else {}),
                **({"annotations": self.annotations} if self.annotations else {}),
            },
            "spec": spec,
        }
