"""Typed AST node dataclasses extracted from tree-sitter parse trees."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class NodeKind(str, Enum):
    """Discriminator for top-level AST nodes."""

    CLASS = "class"
    INTERFACE = "interface"
    ENUM = "enum"
    METHOD = "method"
    FIELD = "field"
    IMPORT = "import"
    PACKAGE = "package"
    EXPRESSION = "expression"
    STATEMENT = "statement"
    CONSTRUCTOR = "constructor"
    ANNOTATION = "annotation"
    COMMENT = "comment"


@dataclass
class SourceLocation:
    """Source position for traceability."""

    start_line: int = 0
    end_line: int = 0
    start_col: int = 0
    end_col: int = 0


@dataclass
class AnnotationNode:
    """A Groovy annotation (e.g. @Override, @NonCPS)."""

    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    location: SourceLocation = field(default_factory=SourceLocation)


@dataclass
class TypeRef:
    """A type reference (e.g. String, List<String>, Map<String, Object>)."""

    name: str = ""
    generic_params: list[TypeRef] = field(default_factory=list)
    is_array: bool = False

    def __str__(self) -> str:
        if self.generic_params:
            params = ", ".join(str(p) for p in self.generic_params)
            base = f"{self.name}<{params}>"
        else:
            base = self.name
        return f"{base}[]" if self.is_array else base


@dataclass
class ParameterNode:
    """A method/constructor parameter."""

    name: str = ""
    type_ref: TypeRef | None = None
    default_value: str | None = None
    annotations: list[AnnotationNode] = field(default_factory=list)
    location: SourceLocation = field(default_factory=SourceLocation)


@dataclass
class ExpressionNode:
    """A generic expression — captures the raw text and detected sub-type."""

    kind: str = ""  # e.g. "string_literal", "method_call", "elvis", "safe_nav", etc.
    text: str = ""
    children: list[ExpressionNode] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    location: SourceLocation = field(default_factory=SourceLocation)


@dataclass
class StatementNode:
    """A statement in a method body."""

    kind: str = ""  # e.g. "return", "if", "for", "expression", "variable_declaration"
    text: str = ""
    children: list[StatementNode | ExpressionNode] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    location: SourceLocation = field(default_factory=SourceLocation)


@dataclass
class FieldDecl:
    """A class field declaration."""

    name: str = ""
    type_ref: TypeRef | None = None
    modifiers: list[str] = field(default_factory=list)
    default_value: str | None = None
    annotations: list[AnnotationNode] = field(default_factory=list)
    javadoc: str | None = None
    location: SourceLocation = field(default_factory=SourceLocation)


@dataclass
class MethodDecl:
    """A method declaration."""

    name: str = ""
    return_type: TypeRef | None = None
    parameters: list[ParameterNode] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)
    annotations: list[AnnotationNode] = field(default_factory=list)
    body_text: str = ""
    body_statements: list[StatementNode] = field(default_factory=list)
    javadoc: str | None = None
    is_constructor: bool = False
    location: SourceLocation = field(default_factory=SourceLocation)


@dataclass
class EnumValue:
    """A single enum constant."""

    name: str = ""
    arguments: list[str] = field(default_factory=list)
    javadoc: str | None = None
    location: SourceLocation = field(default_factory=SourceLocation)


@dataclass
class ImportDecl:
    """An import declaration."""

    path: str = ""
    alias: str | None = None
    is_static: bool = False
    is_wildcard: bool = False
    location: SourceLocation = field(default_factory=SourceLocation)


@dataclass
class ClassDecl:
    """A class declaration (concrete or abstract)."""

    kind: NodeKind = NodeKind.CLASS
    name: str = ""
    package: str = ""
    modifiers: list[str] = field(default_factory=list)
    extends: str | None = None
    implements: list[str] = field(default_factory=list)
    annotations: list[AnnotationNode] = field(default_factory=list)
    fields: list[FieldDecl] = field(default_factory=list)
    methods: list[MethodDecl] = field(default_factory=list)
    constructors: list[MethodDecl] = field(default_factory=list)
    inner_classes: list[ClassDecl] = field(default_factory=list)
    javadoc: str | None = None
    location: SourceLocation = field(default_factory=SourceLocation)


@dataclass
class InterfaceDecl:
    """An interface declaration."""

    kind: NodeKind = NodeKind.INTERFACE
    name: str = ""
    package: str = ""
    modifiers: list[str] = field(default_factory=list)
    extends: list[str] = field(default_factory=list)
    annotations: list[AnnotationNode] = field(default_factory=list)
    fields: list[FieldDecl] = field(default_factory=list)
    methods: list[MethodDecl] = field(default_factory=list)
    javadoc: str | None = None
    location: SourceLocation = field(default_factory=SourceLocation)


@dataclass
class EnumDecl:
    """An enum declaration."""

    kind: NodeKind = NodeKind.ENUM
    name: str = ""
    package: str = ""
    modifiers: list[str] = field(default_factory=list)
    implements: list[str] = field(default_factory=list)
    annotations: list[AnnotationNode] = field(default_factory=list)
    values: list[EnumValue] = field(default_factory=list)
    fields: list[FieldDecl] = field(default_factory=list)
    methods: list[MethodDecl] = field(default_factory=list)
    javadoc: str | None = None
    location: SourceLocation = field(default_factory=SourceLocation)


@dataclass
class GroovySourceFile:
    """Top-level representation of a parsed Groovy file."""

    file_path: str = ""
    package: str = ""
    imports: list[ImportDecl] = field(default_factory=list)
    classes: list[ClassDecl | InterfaceDecl | EnumDecl] = field(default_factory=list)
    top_level_methods: list[MethodDecl] = field(default_factory=list)
    top_level_statements: list[StatementNode] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)
    # Step-2 only: set when the regex fallback replaced or merged into the AST.
    used_fallback: bool = False
    fallback_details: dict[str, Any] = field(default_factory=dict)
