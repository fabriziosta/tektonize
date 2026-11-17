"""Jenkins Pipeline global bindings (names only) and unbound param harvest.

tektonize never loads Jenkins controller/job parameter *values*. Binding
concrete values into PipelineRuns is left to a future job-invoker. This module
only:

1. Names well-known Pipeline globals (``params``, ``env``, …) so validators do
   not treat ``params.FOO`` / ``env.BAR`` as invalid Groovy camelCase.
2. Harvests job-parameter *names* from ``params.FOO`` / ``params['FOO']`` into
   an unbound :class:`~tektonize.tekton.param_schema.ParamSchema`.
"""

from __future__ import annotations

import re

from tektonize.tekton.param_schema import ParamSchema

# Well-known Jenkins Pipeline script bindings (identifiers only — no values).
JENKINS_GLOBAL_BINDINGS: frozenset[str] = frozenset(
    {
        "params",
        "env",
        "currentBuild",
        "scm",
        "docker",
    }
)

# Job params are conventionally UPPER_SNAKE; skip Map methods / nested camelCase.
_PARAM_ATTR = re.compile(r"\bparams\.([A-Z][A-Z0-9_]*)\b")
_PARAM_BRACKET = re.compile(r"""\bparams\[\s*(['"])([^'"]+)\1\s*\]""")

_BINDING_NAMES_ALT = "|".join(sorted(JENKINS_GLOBAL_BINDINGS, key=len, reverse=True))
# params.OVERRIDE_NVR / env.JOB_NAME / currentBuild.result
_BINDING_ATTR = re.compile(rf"\b({_BINDING_NAMES_ALT})\.([A-Za-z_][\w]*)")


def is_jenkins_global_binding(name: str) -> bool:
    """Return True if *name* is a known Jenkins Pipeline global binding."""
    return name in JENKINS_GLOBAL_BINDINGS


def mask_jenkins_binding_accesses(text: str) -> str:
    """Replace ``binding.attr`` with a snake_case placeholder for validators.

    ``MethodTransformer`` rejects ``.\\w+[A-Z]\\w*`` as Groovy camelCase; that
    also matches Jenkins ``params.OVERRIDE_NVR`` / ``env.JOB_NAME``. Masking
    keeps those lines as real Python attribute access after validation.
    """
    return _BINDING_ATTR.sub(r"\1.__jenkins_attr__", text)


def harvest_param_names(text: str) -> list[str]:
    """Collect unbound Jenkins job param names from Groovy/Python body text.

    Harvests:

    - ``params.UPPER_SNAKE`` (typical job parameters)
    - ``params['name']`` / ``params[\"name\"]`` (explicit keys)

    Does **not** read or substitute values. Skips lowercase / camelCase
    ``params.foo`` accesses (nested config maps, Map methods like ``sort``).
    """
    if not text:
        return []
    names: set[str] = set()
    names.update(_PARAM_ATTR.findall(text))
    names.update(m.group(2) for m in _PARAM_BRACKET.finditer(text))
    return sorted(names)


def param_schema_from_text(text: str) -> ParamSchema:
    """Build an unbound ParamSchema from harvested ``params.*`` names."""
    return ParamSchema(required=harvest_param_names(text))


def format_param_schema_assignment(schema: ParamSchema, *, name: str = "_PARAM_SCHEMA") -> str:
    """Emit a Python assignment for an unbound ParamSchema (no values)."""
    required = ", ".join(repr(n) for n in schema.required)
    return f"{name} = ParamSchema(required=[{required}])"
