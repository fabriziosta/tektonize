"""Transform Groovy imports to Python imports and generate __init__.py files."""

from __future__ import annotations

from dataclasses import dataclass, field

from tektonize.parser.ast_nodes import ImportDecl
from tektonize.transformer.name_converter import (
    groovy_import_to_python_module,
)


@dataclass
class PythonImport:
    """A Python import statement."""

    module: str = ""
    symbol: str = ""
    alias: str | None = None
    is_from_import: bool = True
    comment: str = ""

    def to_line(self) -> str:
        if self.alias:
            if self.is_from_import:
                return f"from {self.module} import {self.symbol} as {self.alias}"
            return f"import {self.module} as {self.alias}"
        if self.is_from_import:
            return f"from {self.module} import {self.symbol}"
        return f"import {self.module}"


@dataclass
class ImportTransformResult:
    """Result of transforming all imports for a file."""

    stdlib_imports: list[PythonImport] = field(default_factory=list)
    third_party_imports: list[PythonImport] = field(default_factory=list)
    local_imports: list[PythonImport] = field(default_factory=list)
    extra_imports: list[str] = field(default_factory=list)

    def to_lines(self) -> list[str]:
        """Generate import lines grouped by category with blank separators."""
        groups = []

        if self.extra_imports:
            groups.append(self.extra_imports)

        stdlib = [i.to_line() for i in self.stdlib_imports]
        if stdlib:
            groups.append(sorted(set(stdlib)))

        third = [i.to_line() for i in self.third_party_imports]
        if third:
            groups.append(sorted(set(third)))

        local = [i.to_line() for i in self.local_imports]
        if local:
            groups.append(sorted(set(local)))

        lines = []
        for i, group in enumerate(groups):
            if i > 0:
                lines.append("")
            lines.extend(group)

        return lines


# Known Java/Groovy -> Python stdlib mappings
STDLIB_IMPORT_MAP: dict[str, PythonImport] = {
    "java.util.UUID": PythonImport(module="uuid", symbol="UUID", is_from_import=True),
    "java.util.Date": PythonImport(module="datetime", symbol="datetime", is_from_import=True),
    "java.util.regex.Pattern": PythonImport(module="re", symbol="", is_from_import=False),
    "java.util.regex.Matcher": PythonImport(module="re", symbol="", is_from_import=False),
    "groovy.json.JsonSlurper": PythonImport(module="json", symbol="", is_from_import=False),
    "groovy.json.JsonOutput": PythonImport(module="json", symbol="", is_from_import=False),
}

# Imports to skip entirely (Jenkins/Groovy internals)
SKIP_IMPORTS: set[str] = {
    "jenkins.model.Jenkins",
    "hudson.model.Result",
    "org.jenkinsci.plugins.workflow.cps.CpsScript",
    "com.cloudbees.groovy.cps.NonCPS",
    "groovy.transform.CompileStatic",
    "groovy.transform.TypeChecked",
    "groovy.transform.PackageScope",
    "java.io.Serializable",
}


class ImportTransformer:
    """Transform Groovy imports to Python imports."""

    def __init__(self, source_package: str = "") -> None:
        self.source_package = source_package

    def transform_imports(self, imports: list[ImportDecl]) -> ImportTransformResult:
        result = ImportTransformResult()

        for imp in imports:
            path = imp.path.strip().rstrip(";")

            # Skip Jenkins/Groovy internals
            if path in SKIP_IMPORTS or any(
                path.startswith(p)
                for p in (
                    "jenkins.",
                    "hudson.",
                    "org.jenkinsci.",
                    "com.cloudbees.",
                )
            ):
                continue

            # Check stdlib mapping
            if path in STDLIB_IMPORT_MAP:
                result.stdlib_imports.append(STDLIB_IMPORT_MAP[path])
                continue

            # Java stdlib -> skip or map
            if path.startswith("java.") or path.startswith("javax."):
                self._map_java_import(path, result)
                continue

            # Groovy imports -> skip or map
            if path.startswith("groovy."):
                continue

            # Wildcard imports -> skip
            if imp.is_wildcard:
                continue

            # Remaining imports are treated as local shared-library packages
            result.local_imports.append(self._transform_library_import(path, imp.alias))

        return result

    def _transform_library_import(self, path: str, alias: str | None) -> PythonImport:
        """Transform a Groovy library import to a Python import."""
        module, symbol = groovy_import_to_python_module(path)
        return PythonImport(
            module=module,
            symbol=symbol,
            alias=alias,
            is_from_import=True,
        )

    def _map_java_import(self, path: str, result: ImportTransformResult) -> None:
        """Map common Java imports to Python equivalents."""
        if "List" in path or "ArrayList" in path:
            return  # Python list is builtin
        if "Map" in path or "HashMap" in path:
            return  # Python dict is builtin
        if "Set" in path or "HashSet" in path:
            return  # Python set is builtin
        if "Collections" in path:
            return
        if "Arrays" in path:
            return
        if "Math" in path:
            result.stdlib_imports.append(
                PythonImport(module="math", symbol="", is_from_import=False)
            )


def generate_init_py(package_path: str, modules: list[str]) -> str:
    """Generate an __init__.py file for a Python package.

    Args:
        package_path: dot-separated package path
        modules: list of module names (without .py)

    Returns:
        Content for __init__.py
    """
    lines = [f'"""Package {package_path}."""', ""]

    # Generate __all__ with the module names
    if modules:
        exports = sorted(modules)
        lines.append("__all__ = [")
        for mod in exports:
            lines.append(f'    "{mod}",')
        lines.append("]")
        lines.append("")

    return "\n".join(lines)
