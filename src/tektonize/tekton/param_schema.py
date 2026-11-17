"""Parameter schema, secret references, and workspace references for Tekton tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SecretRef:
    """Reference to a Kubernetes Secret for use in task steps."""

    secret_name: str = ""
    keys: list[str] = field(default_factory=list)
    env_prefix: str = ""
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"secretName": self.secret_name}
        if self.keys:
            result["keys"] = self.keys
        if self.env_prefix:
            result["envPrefix"] = self.env_prefix
        if self.optional:
            result["optional"] = True
        return result


@dataclass
class WorkspaceRef:
    """Reference to a Tekton workspace."""

    name: str = ""
    description: str = ""
    optional: bool = False
    mount_path: str = ""
    read_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"name": self.name}
        if self.description:
            result["description"] = self.description
        if self.optional:
            result["optional"] = True
        if self.mount_path:
            result["mountPath"] = self.mount_path
        if self.read_only:
            result["readOnly"] = True
        return result


@dataclass
class ParamSchema:
    """Parameter schema for a Tekton task or pipeline fragment."""

    required: list[str] = field(default_factory=list)
    optional: dict[str, str] = field(default_factory=dict)
    secrets: list[SecretRef] = field(default_factory=list)
    workspaces: list[WorkspaceRef] = field(default_factory=list)

    def to_params_list(self) -> list[dict[str, Any]]:
        params = []
        for name in self.required:
            params.append({"name": name, "type": "string"})
        for name, default in self.optional.items():
            params.append({"name": name, "type": "string", "default": default})
        return params
