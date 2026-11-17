"""Tekton PipelineFragment, TaskRef, and WhenCondition.

A PipelineFragment is an ordered list of task references with conditions.
Scenarios produce these. They are composable but intentionally incomplete —
the job-invoker converter adds bindings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WhenCondition:
    """A 'when' condition on a task reference."""

    input_param: str = ""
    operator: str = "in"  # "in" or "notin"
    values: list[str] = field(default_factory=list)
    expression: str = ""  # raw Python expression for complex conditions

    def to_dict(self) -> dict[str, Any]:
        if self.expression:
            return {"expression": self.expression}
        return {
            "input": f"$(params.{self.input_param})",
            "operator": self.operator,
            "values": self.values,
        }


@dataclass
class TaskRef:
    """A reference to a task within a pipeline fragment."""

    name: str = ""
    task_ref: str = ""  # Name of the TaskDefinition
    run_after: list[str] = field(default_factory=list)
    when: list[WhenCondition] = field(default_factory=list)
    params: dict[str, str] = field(default_factory=dict)
    workspaces: list[dict[str, str]] = field(default_factory=list)
    on_error: str = ""
    timeout: str = ""
    retries: int = 0

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "taskRef": {"name": self.task_ref or self.name},
        }
        if self.run_after:
            result["runAfter"] = self.run_after
        if self.when:
            result["when"] = [w.to_dict() for w in self.when]
        if self.params:
            result["params"] = [{"name": k, "value": v} for k, v in self.params.items()]
        if self.workspaces:
            result["workspaces"] = self.workspaces
        if self.on_error:
            result["onError"] = self.on_error
        if self.timeout:
            result["timeout"] = self.timeout
        if self.retries:
            result["retries"] = self.retries
        return result


@dataclass
class PipelineFragment:
    """An ordered list of task references forming a partial pipeline.

    Fragments are composable but intentionally incomplete — they declare
    what params/secrets they need without binding specific values.
    """

    name: str = ""
    description: str = ""
    tasks: list[TaskRef] = field(default_factory=list)
    finally_tasks: list[TaskRef] = field(default_factory=list)
    required_params: list[str] = field(default_factory=list)
    optional_params: dict[str, str] = field(default_factory=dict)
    workspaces: list[str] = field(default_factory=list)
    source_class: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
        }
        if self.description:
            result["description"] = self.description
        result["tasks"] = [t.to_dict() for t in self.tasks]
        if self.finally_tasks:
            result["finally"] = [t.to_dict() for t in self.finally_tasks]
        if self.required_params:
            result["params"] = [{"name": p} for p in self.required_params]
        if self.workspaces:
            result["workspaces"] = [{"name": w} for w in self.workspaces]
        return result
