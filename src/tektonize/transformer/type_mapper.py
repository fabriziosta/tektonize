"""Map Groovy types to Python types."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tektonize.parser.ast_nodes import AnnotationNode, TypeRef
from tektonize.parser.normalize import (
    EMPTY_LIST_SENTINEL,
    EMPTY_MAP_SENTINEL,
    EMPTY_STR_SENTINEL,
)

# Core type mapping: Groovy type name -> Python type name
GROOVY_TO_PYTHON_TYPES: dict[str, str] = {
    "String": "str",
    "string": "str",
    "Integer": "int",
    "int": "int",
    "Long": "int",
    "long": "int",
    "Float": "float",
    "float": "float",
    "Double": "float",
    "double": "float",
    "Boolean": "bool",
    "boolean": "bool",
    "Object": "Any",
    "Map": "dict",
    "HashMap": "dict",
    "LinkedHashMap": "dict",
    "List": "list",
    "ArrayList": "list",
    "Set": "set",
    "HashSet": "set",
    "Collection": "list",
    "Iterable": "list",
    "void": "None",
    "Void": "None",
    "def": "",
    "Closure": "Callable",
    "Class": "type",
    "Exception": "Exception",
    "Throwable": "Exception",
    "Number": "int | float",
    "BigDecimal": "float",
    "BigInteger": "int",
    "CharSequence": "str",
    "GString": "str",
    "Pattern": "re.Pattern",
    "Matcher": "re.Match",
    "Date": "datetime",
    "UUID": "uuid.UUID",
}

# Annotations to drop entirely (no Python equivalent needed)
DROP_ANNOTATIONS: set[str] = {
    "Override",
    "NonCPS",
    "Serializable",
    "SuppressWarnings",
    "Deprecated",
    "CompileStatic",
    "TypeChecked",
    "PackageScope",
    "Lazy",
    "Delegate",
    "Newify",
    "Canonical",
    "InheritConstructors",
    "EqualsAndHashCode",
    "ToString",
    "Sortable",
    "Builder",
    "TupleConstructor",
}

# Interfaces/classes to drop from extends/implements
DROP_INTERFACES: set[str] = {
    "Serializable",
    "Cloneable",
    "Comparable",
    "Externalizable",
    "GroovyObject",
    "GroovyInterceptable",
}


@dataclass
class TypeMapping:
    """Result of mapping a Groovy type to Python."""

    python_type: str = ""
    needs_import: str | None = None  # e.g. "from typing import Any"
    is_unknown: bool = False


@dataclass
class TypeMapperResult:
    """Accumulated imports needed after mapping all types in a file."""

    typing_imports: set[str] = field(default_factory=set)
    stdlib_imports: set[str] = field(default_factory=set)


class TypeMapper:
    """Map Groovy types and annotations to Python equivalents."""

    def __init__(self) -> None:
        self._result = TypeMapperResult()

    @property
    def result(self) -> TypeMapperResult:
        return self._result

    def map_type(self, type_ref: TypeRef | None) -> str:
        """Map a Groovy type to its Python equivalent."""
        if type_ref is None:
            return ""

        name = type_ref.name.strip()

        if not name or name == "def":
            return ""

        # Handle inline generics in name (e.g. "List<String>" from fallback parser)
        import re

        generic_match = re.match(r"^(\w+)\s*<(.+)>$", name)
        if generic_match and not type_ref.generic_params:
            base_name = generic_match.group(1)
            inner_text = generic_match.group(2)
            inner_parts = [p.strip() for p in inner_text.split(",")]
            type_ref = TypeRef(
                name=base_name,
                generic_params=[TypeRef(name=p) for p in inner_parts],
                is_array=type_ref.is_array,
            )
            name = base_name

        # Check direct mapping
        if name in GROOVY_TO_PYTHON_TYPES:
            python_type = GROOVY_TO_PYTHON_TYPES[name]
            self._track_import(python_type)
            return self._apply_generics(python_type, type_ref)

        # Handle generic params for unmapped types
        if type_ref.generic_params:
            params = ", ".join(self.map_type(p) or "Any" for p in type_ref.generic_params)
            self._result.typing_imports.add("Any")
            return f"{name}[{params}]"

        if type_ref.is_array:
            inner = GROOVY_TO_PYTHON_TYPES.get(name, name)
            self._track_import(inner)
            return f"list[{inner}]"

        return name

    def _apply_generics(self, python_type: str, type_ref: TypeRef) -> str:
        if not type_ref.generic_params:
            if type_ref.is_array:
                return f"list[{python_type}]"
            return python_type

        params = []
        for gp in type_ref.generic_params:
            mapped = self.map_type(gp)
            params.append(mapped if mapped else "Any")

        if python_type == "dict" and len(params) == 2:
            return f"dict[{params[0]}, {params[1]}]"
        elif python_type == "dict" and len(params) == 1:
            return f"dict[str, {params[0]}]"
        elif python_type in ("list", "set"):
            return f"{python_type}[{params[0]}]"
        else:
            return f"{python_type}[{', '.join(params)}]"

    def _track_import(self, python_type: str) -> None:
        if python_type == "Any":
            self._result.typing_imports.add("Any")
        elif python_type == "Callable":
            self._result.typing_imports.add("Callable")
        elif python_type == "re.Pattern" or python_type == "re.Match":
            self._result.stdlib_imports.add("re")
        elif python_type == "datetime":
            self._result.stdlib_imports.add("datetime")
        elif python_type == "uuid.UUID":
            self._result.stdlib_imports.add("uuid")

    def should_drop_annotation(self, annotation: AnnotationNode) -> bool:
        """Check if an annotation should be dropped in Python output."""
        return annotation.name in DROP_ANNOTATIONS

    def should_drop_interface(self, name: str) -> bool:
        """Check if an interface should be dropped from implements list."""
        return name in DROP_INTERFACES

    def filter_implements(self, implements: list[str]) -> list[str]:
        """Filter out interfaces that have no Python equivalent."""
        return [i for i in implements if not self.should_drop_interface(i)]

    def map_default_value(self, value: str | None, type_ref: TypeRef | None) -> str | None:
        """Map a Groovy default value to Python."""
        if value is None:
            return None

        value = value.strip()

        # Groovy -> Python literal mappings
        replacements = {
            "null": "None",
            "true": "True",
            "false": "False",
            "[:]": "{}",
            EMPTY_MAP_SENTINEL: "{}",
            EMPTY_LIST_SENTINEL: "[]",
            EMPTY_STR_SENTINEL: '""',
            "[]": "[]",
            "''": '""',
            '""': '""',
        }

        if value in replacements:
            return replacements[value]

        # Numeric suffixes: 1L, 2.0d
        m = re.fullmatch(r"(\d+)[Ll]", value)
        if m:
            return m.group(1)
        m = re.fullmatch(r"(\d+\.\d+)[DdFf]", value)
        if m:
            return m.group(1)

        # Single-quoted strings -> double-quoted (no nested quotes)
        if (
            value.startswith("'")
            and value.endswith("'")
            and value.count("'") == 2
            and "\n" not in value
        ):
            inner = value[1:-1]
            return f'"{inner}"'

        # Complex ``[…]`` / multiline defaults are handled by
        # ``PythonEmitter._sanitize_literal`` via ExpressionTransformer
        # (maps vs lists cannot be distinguished by brackets alone).
        return value

    def get_import_lines(self) -> list[str]:
        """Generate the import lines needed for mapped types."""
        lines = []
        if self._result.typing_imports:
            imports = sorted(self._result.typing_imports)
            lines.append(f"from typing import {', '.join(imports)}")
        for mod in sorted(self._result.stdlib_imports):
            lines.append(f"import {mod}")
        return lines
