"""Transform Groovy class/interface/enum declarations to Python equivalents.

Detects patterns:
- enum -> Enum
- interface -> ABC with @abstractmethod
- Settings POJO -> @dataclass
- abstract class -> ABC subclass
- regular class -> class with __init__
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tektonize.parser.ast_nodes import (
    ClassDecl,
    EnumDecl,
    FieldDecl,
    InterfaceDecl,
    MethodDecl,
    NodeKind,
)
from tektonize.transformer.name_converter import (
    camel_to_snake,
    convert_identifier,
)
from tektonize.transformer.type_mapper import TypeMapper


@dataclass
class PythonClass:
    """Intermediate representation of a transformed Python class."""

    name: str = ""
    kind: str = ""  # "enum", "dataclass", "abc", "class"
    bases: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    fields: list[PythonField] = field(default_factory=list)
    methods: list[PythonMethod] = field(default_factory=list)
    enum_values: list[PythonEnumValue] = field(default_factory=list)
    docstring: str | None = None
    source_kind: str = ""
    source_name: str = ""
    inner_classes: list[PythonClass] = field(default_factory=list)
    extra_imports: list[str] = field(default_factory=list)


@dataclass
class PythonField:
    name: str = ""
    type_hint: str = ""
    default_value: str | None = None
    docstring: str | None = None
    is_class_var: bool = False


@dataclass
class PythonMethod:
    name: str = ""
    params: list[PythonParam] = field(default_factory=list)
    return_type: str = ""
    body_lines: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    docstring: str | None = None
    is_static: bool = False
    is_classmethod: bool = False
    is_abstract: bool = False
    is_property: bool = False
    raw_body: str = ""


@dataclass
class PythonParam:
    name: str = ""
    type_hint: str = ""
    default_value: str | None = None
    # Original Groovy formal name (before snake_case); used to rewrite body uses.
    source_name: str | None = None


@dataclass
class PythonEnumValue:
    name: str = ""
    value: str = "auto()"
    docstring: str | None = None


class ClassTransformer:
    """Transform Groovy class declarations to Python equivalents."""

    def __init__(self, type_mapper: TypeMapper | None = None) -> None:
        self.type_mapper = type_mapper or TypeMapper()

    def transform(self, decl: ClassDecl | InterfaceDecl | EnumDecl) -> PythonClass:
        if isinstance(decl, EnumDecl) or (
            isinstance(decl, ClassDecl) and decl.kind == NodeKind.ENUM
        ):
            return self._transform_enum(decl)
        elif isinstance(decl, InterfaceDecl) or decl.kind == NodeKind.INTERFACE:
            return self._transform_interface(decl)
        elif self._is_settings_pojo(decl):
            return self._transform_dataclass(decl)
        elif "abstract" in getattr(decl, "modifiers", []):
            return self._transform_abstract_class(decl)
        else:
            return self._transform_class(decl)

    def _transform_enum(self, decl: EnumDecl | ClassDecl) -> PythonClass:
        values = getattr(decl, "values", [])
        py = PythonClass(
            name=decl.name,
            kind="enum",
            bases=["Enum"],
            source_kind="enum",
            source_name=decl.name,
            docstring=self._clean_javadoc(decl.javadoc),
            extra_imports=["from enum import Enum, auto"],
        )
        for v in values:
            py.enum_values.append(
                PythonEnumValue(
                    name=v.name,
                    value="auto()",
                    docstring=self._clean_javadoc(v.javadoc) if v.javadoc else None,
                )
            )

        # Add any methods the enum has
        for m in getattr(decl, "methods", []):
            py.methods.append(self._transform_method(m))

        return py

    def _transform_interface(self, decl: InterfaceDecl | ClassDecl) -> PythonClass:
        bases = ["ABC"]
        extends = getattr(decl, "extends", [])
        if isinstance(extends, list):
            for e in extends:
                if e and not self.type_mapper.should_drop_interface(e):
                    bases.insert(0, e)
        elif extends and not self.type_mapper.should_drop_interface(extends):
            bases.insert(0, extends)

        py = PythonClass(
            name=decl.name,
            kind="abc",
            bases=bases,
            source_kind="interface",
            source_name=decl.name,
            docstring=self._clean_javadoc(decl.javadoc),
            extra_imports=[
                "from abc import ABC, abstractmethod",
            ],
        )

        # Interface fields become abstract properties or class vars
        for f in decl.fields:
            if f.default_value is not None:
                py.fields.append(self._transform_field(f, is_class_var=True))
            else:
                py.fields.append(self._transform_field(f))

        # Interface methods become abstract methods
        for m in decl.methods:
            pm = self._transform_method(m)
            pm.is_abstract = True
            pm.decorators.insert(0, "@abstractmethod")
            if not pm.body_lines or pm.body_lines == ["pass"]:
                pm.body_lines = ["..."]
            py.methods.append(pm)

        return py

    def _transform_dataclass(self, decl: ClassDecl) -> PythonClass:
        bases = []
        if decl.extends and not self.type_mapper.should_drop_interface(decl.extends):
            bases.append(decl.extends)
        if hasattr(decl, "implements"):
            for i in self.type_mapper.filter_implements(decl.implements):
                bases.append(i)

        py = PythonClass(
            name=decl.name,
            kind="dataclass",
            bases=bases,
            decorators=["@dataclass"],
            source_kind="class",
            source_name=decl.name,
            docstring=self._clean_javadoc(decl.javadoc),
            extra_imports=["from dataclasses import dataclass, field"],
        )

        for f in decl.fields:
            py.fields.append(self._transform_field(f))

        for m in decl.methods:
            pm = self._transform_method(m)
            # Convert static factory methods to classmethods
            if "static" in m.modifiers and m.name.startswith("from"):
                pm.is_classmethod = True
                pm.decorators = ["@classmethod"]
                if pm.params and pm.params[0].name != "cls":
                    pm.params.insert(0, PythonParam(name="cls"))
            py.methods.append(pm)

        for c in getattr(decl, "constructors", []):
            py.methods.append(self._transform_constructor(c, decl))

        return py

    def _transform_abstract_class(self, decl: ClassDecl) -> PythonClass:
        bases = ["ABC"]
        if decl.extends and not self.type_mapper.should_drop_interface(decl.extends):
            bases.insert(0, decl.extends)
        if hasattr(decl, "implements"):
            for i in self.type_mapper.filter_implements(decl.implements):
                if i not in bases:
                    bases.append(i)

        py = PythonClass(
            name=decl.name,
            kind="abc",
            bases=bases,
            source_kind="abstract_class",
            source_name=decl.name,
            docstring=self._clean_javadoc(decl.javadoc),
            extra_imports=["from abc import ABC, abstractmethod"],
        )

        for f in decl.fields:
            py.fields.append(self._transform_field(f))

        for m in decl.methods:
            pm = self._transform_method(m)
            if "abstract" in m.modifiers:
                pm.is_abstract = True
                pm.decorators.insert(0, "@abstractmethod")
                pm.body_lines = ["..."]
            py.methods.append(pm)

        for c in getattr(decl, "constructors", []):
            py.methods.append(self._transform_constructor(c, decl))

        return py

    def _transform_class(self, decl: ClassDecl) -> PythonClass:
        bases = []
        if decl.extends and not self.type_mapper.should_drop_interface(decl.extends):
            bases.append(decl.extends)
        if hasattr(decl, "implements"):
            for i in self.type_mapper.filter_implements(decl.implements):
                if i not in bases:
                    bases.append(i)

        py = PythonClass(
            name=decl.name,
            kind="class",
            bases=bases,
            source_kind="class",
            source_name=decl.name,
            docstring=self._clean_javadoc(decl.javadoc),
        )

        for f in decl.fields:
            py.fields.append(self._transform_field(f))

        for m in decl.methods:
            py.methods.append(self._transform_method(m))

        for c in getattr(decl, "constructors", []):
            py.methods.append(self._transform_constructor(c, decl))

        # Add inner classes
        for inner in getattr(decl, "inner_classes", []):
            py.inner_classes.append(self.transform(inner))

        return py

    def _transform_field(self, f: FieldDecl, is_class_var: bool = False) -> PythonField:
        type_hint = self.type_mapper.map_type(f.type_ref) if f.type_ref else ""
        default = self.type_mapper.map_default_value(f.default_value, f.type_ref)

        python_name = convert_identifier(f.name)

        return PythonField(
            name=python_name,
            type_hint=type_hint,
            default_value=default,
            docstring=self._clean_javadoc(f.javadoc),
            is_class_var=is_class_var or "static" in f.modifiers,
        )

    def _transform_method(self, m: MethodDecl) -> PythonMethod:
        return_type = self.type_mapper.map_type(m.return_type) if m.return_type else ""
        is_static = "static" in m.modifiers

        params = []
        if not is_static:
            params.append(PythonParam(name="self"))

        for p in m.parameters:
            type_hint = self.type_mapper.map_type(p.type_ref) if p.type_ref else ""
            default = self.type_mapper.map_default_value(p.default_value, p.type_ref)
            params.append(
                PythonParam(
                    name=convert_identifier(p.name),
                    type_hint=type_hint,
                    default_value=default,
                    source_name=p.name,
                )
            )

        decorators = []
        if is_static:
            decorators.append("@staticmethod")

        return PythonMethod(
            name=camel_to_snake(m.name),
            params=params,
            return_type=return_type,
            body_lines=["pass"],
            decorators=decorators,
            docstring=self._clean_javadoc(m.javadoc),
            is_static=is_static,
            raw_body=m.body_text,
        )

    def _transform_constructor(self, c: MethodDecl, decl: ClassDecl) -> PythonMethod:
        params = [PythonParam(name="self")]
        for p in c.parameters:
            type_hint = self.type_mapper.map_type(p.type_ref) if p.type_ref else ""
            default = self.type_mapper.map_default_value(p.default_value, p.type_ref)
            params.append(
                PythonParam(
                    name=convert_identifier(p.name),
                    type_hint=type_hint,
                    default_value=default,
                    source_name=p.name,
                )
            )

        return PythonMethod(
            name="__init__",
            params=params,
            return_type="None",
            body_lines=["pass"],
            docstring=self._clean_javadoc(c.javadoc),
            raw_body=c.body_text,
        )

    def _is_settings_pojo(self, decl: ClassDecl) -> bool:
        """Detect if a class is a settings POJO (-> @dataclass)."""
        if not isinstance(decl, ClassDecl):
            return False
        # Heuristics: has fields, possibly has fromMap/toMap, implements Serializable
        has_serializable = "Serializable" in getattr(decl, "implements", [])
        has_fields = len(decl.fields) >= 2
        has_from_map = any(m.name == "fromMap" for m in decl.methods)
        has_to_map = any(m.name == "toMap" for m in decl.methods)

        # Settings pattern: many fields + Serializable + optional fromMap/toMap
        if has_serializable and has_fields:
            return True
        if has_from_map or has_to_map:
            return True
        # Name ends with "Settings"
        return bool(decl.name.endswith("Settings"))

    def _clean_javadoc(self, javadoc: str | None) -> str | None:
        """Convert Javadoc comment to Python docstring content."""
        if not javadoc:
            return None

        lines = javadoc.split("\n")
        cleaned = []
        for line in lines:
            line = line.strip()
            # Remove comment markers
            line = line.removeprefix("/**").removeprefix("*/").removeprefix("*")
            line = line.removesuffix("*/")
            line = line.strip()

            # Convert @param tags
            if line.startswith("@param "):
                parts = line[7:].split(None, 1)
                if len(parts) == 2:
                    cleaned.append("Args:")
                    cleaned.append(f"    {camel_to_snake(parts[0])}: {parts[1]}")
                continue

            # Convert @return tags
            if line.startswith("@return"):
                text = line[7:].strip()
                cleaned.append("Returns:")
                cleaned.append(f"    {text}")
                continue

            # Skip empty @tags
            if line.startswith("@"):
                continue

            if line:
                cleaned.append(line)

        return "\n".join(cleaned).strip() or None
