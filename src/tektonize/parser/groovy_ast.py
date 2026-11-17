"""Tree-sitter based Groovy parser that extracts typed AST nodes."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import tree_sitter_groovy as tsgroovy
from tree_sitter import Language, Node, Parser

from tektonize.parser.ast_nodes import (
    AnnotationNode,
    ClassDecl,
    EnumDecl,
    EnumValue,
    FieldDecl,
    GroovySourceFile,
    ImportDecl,
    InterfaceDecl,
    MethodDecl,
    NodeKind,
    ParameterNode,
    SourceLocation,
    StatementNode,
    TypeRef,
)
from tektonize.parser.normalize import (  # noqa: F401 — re-export for callers/tests
    EMPTY_MAP_SENTINEL,
    normalize_groovy_for_treesitter,
)
from tektonize.parser.parse_diagnostics import collect_cst_parse_issues

GROOVY_LANGUAGE = Language(tsgroovy.language())


def _loc(node: Node) -> SourceLocation:
    return SourceLocation(
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_col=node.start_point[1],
        end_col=node.end_point[1],
    )


def _text(node: Node | None) -> str:
    if node is None:
        return ""
    return node.text.decode("utf-8") if isinstance(node.text, bytes) else str(node.text)


def _child_by_type(node: Node, type_name: str) -> Node | None:
    for child in node.children:
        if child.type == type_name:
            return child
    return None


def _children_by_type(node: Node, type_name: str) -> list[Node]:
    return [c for c in node.children if c.type == type_name]


def _collect_modifiers(node: Node) -> list[str]:
    modifiers = []
    for child in node.children:
        if child.type == "modifiers" or child.type == "modifier":
            modifiers.extend(_text(child).split())
        elif child.type in (
            "public",
            "private",
            "protected",
            "static",
            "final",
            "abstract",
            "synchronized",
            "transient",
            "volatile",
        ):
            modifiers.append(_text(child))
    return modifiers


def _parse_type_ref(node: Node | None) -> TypeRef | None:
    if node is None:
        return None
    text = _text(node).strip()
    if not text or text == "def" or text == "void":
        return TypeRef(name=text) if text else None

    array = text.endswith("[]")
    if array:
        text = text[:-2]

    match = re.match(r"^(\w+)\s*<(.+)>$", text)
    if match:
        name = match.group(1)
        inner = match.group(2)
        generics = [TypeRef(name=p.strip()) for p in _split_generics(inner)]
        return TypeRef(name=name, generic_params=generics, is_array=array)

    return TypeRef(name=text, is_array=array)


def _split_generics(text: str) -> list[str]:
    """Split generic parameters respecting nested angle brackets."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char == "<":
            depth += 1
            current.append(char)
        elif char == ">":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())
    return parts


def _get_preceding_javadoc(node: Node) -> str | None:
    """Get the javadoc comment immediately preceding a node."""
    if node.prev_named_sibling and node.prev_named_sibling.type == "comment":
        text = _text(node.prev_named_sibling)
        if text.startswith("/**"):
            return text
    return None


class GroovyParser:
    """Parse Groovy source files using tree-sitter and extract typed AST nodes."""

    def __init__(self) -> None:
        self._parser = Parser(GROOVY_LANGUAGE)

    def parse_file(self, path: Path) -> GroovySourceFile:
        source = path.read_text(encoding="utf-8")
        return self.parse_source(source, str(path))

    def parse_source(self, source: str, file_path: str = "<string>") -> GroovySourceFile:
        normalized = normalize_groovy_for_treesitter(source)
        tree = self._parser.parse(normalized.encode("utf-8"))
        root = tree.root_node

        result = GroovySourceFile(file_path=file_path)

        # Collect parse errors with source context (same format as 01_raw_ast)
        raw_tree = self._node_to_dict(root)
        result.parse_errors = [
            issue.summary() for issue in collect_cst_parse_issues(raw_tree, normalized)
        ]

        # Walk top-level children
        for child in root.children:
            self._process_top_level(child, result)

        if not result.parse_errors:
            return result

        from tektonize.parser.fallback_parser import parse_groovy_fallback

        # Fallback uses the original source (regex patterns expect ``[:]``, etc.)
        fallback = parse_groovy_fallback(source, file_path)

        # Prefer fallback when tree-sitter found no classes (even if it mistook
        # class methods for top-level methods after a fragmented class header).
        if not result.classes and fallback.classes:
            if result.imports and not fallback.imports:
                fallback.imports = result.imports
            if result.package and not fallback.package:
                fallback.package = result.package
            fallback.parse_errors = [
                *result.parse_errors,
                "Used fallback regex parser due to tree-sitter errors",
            ]
            fallback.used_fallback = True
            fallback.fallback_details = {
                "mode": "replace",
                "reason": "tree-sitter found no classes",
                "classes_from_fallback": [c.name for c in fallback.classes],
            }
            return fallback

        # If tree-sitter got classes but missed details, merge from fallback
        merged_names: list[str] = []
        if result.classes and fallback.classes:
            for cls in result.classes:
                fb_classes = [fc for fc in fallback.classes if fc.name == cls.name]
                if not fb_classes:
                    continue
                fb = fb_classes[0]
                changed = False
                if not cls.fields and fb.fields:
                    cls.fields = fb.fields
                    changed = True
                if not cls.methods and fb.methods:
                    cls.methods = fb.methods
                    changed = True
                if isinstance(cls, ClassDecl) and isinstance(fb, ClassDecl):
                    if not cls.extends and fb.extends:
                        cls.extends = fb.extends
                        changed = True
                    if not cls.implements and fb.implements:
                        cls.implements = fb.implements
                        changed = True
                    if not cls.constructors and fb.constructors:
                        cls.constructors = fb.constructors
                        changed = True
                if isinstance(cls, EnumDecl) and isinstance(fb, EnumDecl):
                    if not cls.values and fb.values:
                        cls.values = fb.values
                        changed = True
                    elif fb.values and len(fb.values) > len(cls.values):
                        # Prefer the richer constant list from fallback.
                        cls.values = fb.values
                        changed = True
                if changed:
                    merged_names.append(cls.name)

        if merged_names:
            result.used_fallback = True
            result.fallback_details = {
                "mode": "merge",
                "reason": "tree-sitter classes incomplete",
                "merged_classes": merged_names,
            }
            result.parse_errors = [
                *result.parse_errors,
                "Used fallback regex parser due to tree-sitter errors",
            ]

        # vars/-style files: fallback may recover top-level methods tree-sitter missed
        if not result.classes and not result.top_level_methods and fallback.top_level_methods:
            fallback.parse_errors = [
                *result.parse_errors,
                "Used fallback regex parser due to tree-sitter errors",
            ]
            fallback.used_fallback = True
            fallback.fallback_details = {
                "mode": "replace",
                "reason": "tree-sitter found no top-level methods",
                "top_level_methods": [m.name for m in fallback.top_level_methods],
            }
            return fallback

        return result

    def get_raw_tree(self, source: str) -> dict[str, Any]:
        """Return the raw tree-sitter CST as a JSON-serializable dict.

        Applies :func:`normalize_groovy_for_treesitter` so the CST matches what
        :meth:`parse_source` uses for typed extraction.
        """
        normalized = normalize_groovy_for_treesitter(source)
        tree = self._parser.parse(normalized.encode("utf-8"))
        return self._node_to_dict(tree.root_node)

    def _node_to_dict(self, node: Node) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": node.type,
            "start": list(node.start_point),
            "end": list(node.end_point),
        }
        if node.is_missing:
            result["is_missing"] = True
        if not node.children:
            result["text"] = _text(node)
        else:
            result["children"] = [self._node_to_dict(c) for c in node.children]
        return result

    def _process_top_level(self, node: Node, result: GroovySourceFile) -> None:
        if node.type == "package_declaration":
            result.package = self._extract_package(node)
        elif node.type == "import_declaration":
            result.imports.append(self._extract_import(node))
        elif node.type == "class_declaration":
            result.classes.append(self._extract_class(node, result.package))
        elif node.type == "interface_declaration":
            result.classes.append(self._extract_interface(node, result.package))
        elif node.type == "enum_declaration":
            result.classes.append(self._extract_enum(node, result.package))
        elif node.type == "function_definition" or node.type == "method_declaration":
            result.top_level_methods.append(self._extract_method(node))
        elif node.type == "expression_statement" or node.type == "assignment":
            result.top_level_statements.append(self._extract_statement(node))
        elif node.type == "comment":
            result.comments.append(_text(node))

    def _extract_package(self, node: Node) -> str:
        for child in node.children:
            if child.type not in ("package", ";"):
                return _text(child)
        return ""

    def _extract_import(self, node: Node) -> ImportDecl:
        text = _text(node)
        is_static = "static" in text
        path = ""
        alias = None

        # Extract the import path
        for child in node.children:
            if (
                child.type == "dotted_identifier"
                or child.type == "identifier"
                or child.type == "groovy_import"
            ):
                path = _text(child)

        if not path:
            # Fallback: parse from full text
            m = re.search(r"import\s+(?:static\s+)?(\S+?)(?:\s+as\s+(\w+))?\s*$", text)
            if m:
                path = m.group(1)
                alias = m.group(2)

        is_wildcard = path.endswith(".*") or path.endswith("*")

        # Check for alias
        if " as " in text and alias is None:
            parts = text.split(" as ")
            alias = parts[-1].strip().rstrip(";")

        return ImportDecl(
            path=path,
            alias=alias,
            is_static=is_static,
            is_wildcard=is_wildcard,
            location=_loc(node),
        )

    def _extract_class(self, node: Node, package: str) -> ClassDecl:
        cls = ClassDecl(kind=NodeKind.CLASS, package=package, location=_loc(node))
        cls.javadoc = _get_preceding_javadoc(node)
        cls.modifiers = _collect_modifiers(node)

        for child in node.children:
            if child.type == "identifier":
                cls.name = _text(child)
            elif child.type == "superclass" or child.type == "extends_type":
                cls.extends = _text(child).replace("extends", "").strip()
            elif child.type == "super_interfaces" or child.type == "implements_type":
                text = _text(child).replace("implements", "").strip()
                cls.implements = [t.strip() for t in text.split(",")]
            elif child.type == "class_body" or child.type == "body":
                self._extract_class_body(child, cls)
            elif child.type == "annotation":
                cls.annotations.append(self._extract_annotation(child))

        # Handle extends/implements from the direct text if not captured
        if not cls.extends and not cls.implements:
            self._extract_extends_implements_from_text(node, cls)

        return cls

    def _extract_extends_implements_from_text(
        self, node: Node, cls: ClassDecl | InterfaceDecl
    ) -> None:
        """Fallback: parse extends/implements from the declaration text."""
        full_text = _text(node)
        # Only look at the first line or up to the opening brace
        header = full_text.split("{")[0] if "{" in full_text else full_text

        ext_match = re.search(r"\bextends\s+([\w.]+)", header)
        if ext_match:
            if isinstance(cls, ClassDecl):
                cls.extends = ext_match.group(1)
            elif isinstance(cls, InterfaceDecl):
                cls.extends = [ext_match.group(1)]

        impl_match = re.search(r"\bimplements\s+([\w.,\s]+?)(?:\s*\{|$)", header)
        if impl_match:
            impls = [t.strip() for t in impl_match.group(1).split(",") if t.strip()]
            if isinstance(cls, ClassDecl):
                cls.implements = impls

    def _extract_interface(self, node: Node, package: str) -> InterfaceDecl:
        iface = InterfaceDecl(kind=NodeKind.INTERFACE, package=package, location=_loc(node))
        iface.javadoc = _get_preceding_javadoc(node)
        iface.modifiers = _collect_modifiers(node)

        for child in node.children:
            if child.type == "identifier":
                iface.name = _text(child)
            elif child.type == "extends_type" or child.type == "superclass":
                text = _text(child).replace("extends", "").strip()
                iface.extends = [t.strip() for t in text.split(",")]
            elif child.type == "interface_body" or child.type == "body":
                self._extract_interface_body(child, iface)
            elif child.type == "annotation":
                iface.annotations.append(self._extract_annotation(child))

        # Fallback
        if not iface.extends:
            full_text = _text(node)
            header = full_text.split("{")[0]
            ext_match = re.search(r"\bextends\s+([\w.,\s]+?)(?:\s*\{|$)", header)
            if ext_match:
                iface.extends = [t.strip() for t in ext_match.group(1).split(",") if t.strip()]

        return iface

    def _extract_enum(self, node: Node, package: str) -> EnumDecl:
        enum = EnumDecl(kind=NodeKind.ENUM, package=package, location=_loc(node))
        enum.javadoc = _get_preceding_javadoc(node)
        enum.modifiers = _collect_modifiers(node)

        for child in node.children:
            if child.type == "identifier":
                enum.name = _text(child)
            elif child.type == "enum_body" or child.type == "body":
                self._extract_enum_body(child, enum)
            elif child.type == "annotation":
                enum.annotations.append(self._extract_annotation(child))
            elif child.type == "super_interfaces" or child.type == "implements_type":
                text = _text(child).replace("implements", "").strip()
                enum.implements = [t.strip() for t in text.split(",")]

        return enum

    def _enum_value_from_node(self, node: Node, javadoc: str | None) -> EnumValue:
        """Build an EnumValue from an ``enum_constant`` CST node."""
        text = _text(node).strip().rstrip(",").rstrip(";")
        name_m = re.match(r"([A-Za-z_]\w*)", text)
        name = name_m.group(1) if name_m else text
        args: list[str] = []
        args_m = re.search(r"\((.*)\)\s*$", text, re.DOTALL)
        if args_m and args_m.group(1).strip():
            args = [a.strip() for a in args_m.group(1).split(",") if a.strip()]
        return EnumValue(
            name=name,
            arguments=args,
            javadoc=javadoc,
            location=_loc(node),
        )

    def _extract_enum_body(self, node: Node, enum: EnumDecl) -> None:
        prev_comment = None
        for child in node.children:
            if child.type == "comment":
                prev_comment = _text(child) if _text(child).startswith("/**") else None
            elif child.type == "enum_constant":
                ev = self._enum_value_from_node(child, prev_comment)
                prev_comment = None
                enum.values.append(ev)
            elif child.type == "identifier":
                # Some grammars yield bare identifiers for enum constants
                name = _text(child).strip().rstrip(",").rstrip(";")
                if name and name not in ("{", "}", ",") and name.isupper():
                    ev = EnumValue(name=name, javadoc=prev_comment, location=_loc(child))
                    prev_comment = None
                    enum.values.append(ev)
            elif child.type in (
                "method_declaration",
                "function_definition",
            ):
                enum.methods.append(self._extract_method(child))
            elif child.type in (
                "field_declaration",
                "declaration",
                "variable_declaration",
            ):
                enum.fields.append(self._extract_field(child))

    def _extract_class_body(self, node: Node, cls: ClassDecl) -> None:
        prev_comment = None
        for child in node.children:
            if child.type == "comment":
                prev_comment = _text(child) if _text(child).startswith("/**") else None
            elif child.type in ("method_declaration", "function_definition"):
                method = self._extract_method(child)
                if method.javadoc is None and prev_comment:
                    method.javadoc = prev_comment
                prev_comment = None
                if method.is_constructor:
                    cls.constructors.append(method)
                else:
                    cls.methods.append(method)
            elif child.type in (
                "field_declaration",
                "declaration",
                "variable_declaration",
            ):
                fld = self._extract_field(child)
                if fld.javadoc is None and prev_comment:
                    fld.javadoc = prev_comment
                prev_comment = None
                cls.fields.append(fld)
            elif child.type == "class_declaration":
                inner = self._extract_class(child, cls.package)
                cls.inner_classes.append(inner)
            elif child.type == "constructor_declaration":
                ctor = self._extract_method(child)
                ctor.is_constructor = True
                if ctor.javadoc is None and prev_comment:
                    ctor.javadoc = prev_comment
                prev_comment = None
                cls.constructors.append(ctor)
            else:
                prev_comment = None

    def _extract_interface_body(self, node: Node, iface: InterfaceDecl) -> None:
        prev_comment = None
        for child in node.children:
            if child.type == "comment":
                prev_comment = _text(child) if _text(child).startswith("/**") else None
            elif child.type in ("method_declaration", "function_definition"):
                method = self._extract_method(child)
                if method.javadoc is None and prev_comment:
                    method.javadoc = prev_comment
                prev_comment = None
                iface.methods.append(method)
            elif child.type in (
                "field_declaration",
                "declaration",
                "variable_declaration",
            ):
                fld = self._extract_field(child)
                if fld.javadoc is None and prev_comment:
                    fld.javadoc = prev_comment
                prev_comment = None
                iface.fields.append(fld)
            else:
                prev_comment = None

    def _extract_method(self, node: Node) -> MethodDecl:
        method = MethodDecl(location=_loc(node))
        method.javadoc = _get_preceding_javadoc(node)
        method.modifiers = _collect_modifiers(node)
        method.body_text = ""

        for child in node.children:
            if child.type == "identifier":
                if not method.name:
                    method.name = _text(child)
            elif child.type in ("type", "return_type", "generic_type", "builtin_type"):
                method.return_type = _parse_type_ref(child)
            elif child.type in ("formal_parameters", "parameters", "parameter_list"):
                method.parameters = self._extract_parameters(child)
            elif child.type in ("block", "closure", "body"):
                method.body_text = _text(child)
                method.body_statements = self._extract_block_statements(child)
            elif child.type == "annotation":
                method.annotations.append(self._extract_annotation(child))

        # Detect constructor
        if method.name and node.parent:
            parent_name = ""
            for sibling in (node.parent.children if node.parent else []):
                if sibling.type == "identifier":
                    parent_name = _text(sibling)
                    break
            if method.name == parent_name:
                method.is_constructor = True

        return method

    def _extract_field(self, node: Node) -> FieldDecl:
        fld = FieldDecl(location=_loc(node))
        fld.javadoc = _get_preceding_javadoc(node)
        fld.modifiers = _collect_modifiers(node)

        text = _text(node).strip()

        # Try to parse from children first
        for child in node.children:
            if child.type in (
                "type",
                "generic_type",
                "builtin_type",
                "type_identifier",
                "boolean_type",
                "integral_type",
                "floating_point_type",
            ):
                fld.type_ref = _parse_type_ref(child)
            elif child.type == "identifier" and not fld.name:
                fld.name = _text(child)
            elif child.type == "variable_declarator":
                for vc in child.children:
                    if vc.type == "identifier" and not fld.name:
                        fld.name = _text(vc)
                    elif vc.type not in ("=", ";") and fld.default_value is None:
                        val = _text(vc).strip()
                        if val and val != ";":
                            fld.default_value = val
            elif child.type == "annotation":
                fld.annotations.append(self._extract_annotation(child))

        # Fallback: parse field from text
        if not fld.name:
            self._parse_field_from_text(text, fld)

        if fld.default_value:
            fld.default_value = fld.default_value.strip().rstrip(";").strip()

        return fld

    def _parse_field_from_text(self, text: str, fld: FieldDecl) -> None:
        """Parse a field declaration from its text representation."""
        text = text.strip().rstrip(";")

        # Remove annotations
        text = re.sub(r"@\w+(\([^)]*\))?\s*", "", text)

        # Match: [modifiers] Type name [= value]
        m = re.match(
            r"(?:(public|private|protected|static|final)\s+)*"
            r"(?:(\w[\w.<>,\s]*?)\s+)?(\w+)\s*(?:=\s*(.+))?$",
            text,
            re.DOTALL,
        )
        if m:
            type_text = m.group(2)
            fld.name = m.group(3) if m.group(3) else ""
            if type_text and type_text not in ("def",):
                fld.type_ref = TypeRef(name=type_text.strip())
            if m.group(4):
                fld.default_value = m.group(4).strip()

    def _extract_parameters(self, node: Node) -> list[ParameterNode]:
        params = []
        for child in node.children:
            if child.type in ("formal_parameter", "parameter", "simple_parameter"):
                params.append(self._extract_single_param(child))
            elif child.type == "identifier":
                # Bare parameter name without type
                params.append(ParameterNode(name=_text(child), location=_loc(child)))
        return params

    def _extract_single_param(self, node: Node) -> ParameterNode:
        param = ParameterNode(location=_loc(node))
        for child in node.children:
            if child.type in ("type", "generic_type", "builtin_type"):
                param.type_ref = _parse_type_ref(child)
            elif child.type == "identifier":
                if not param.name:
                    param.name = _text(child)
            elif child.type == "annotation":
                param.annotations.append(self._extract_annotation(child))

        # Fallback from text
        if not param.name:
            text = _text(node).strip()
            parts = text.split()
            if parts:
                param.name = parts[-1]
                if len(parts) > 1:
                    param.type_ref = TypeRef(name=" ".join(parts[:-1]))

        # Check for default value
        text = _text(node)
        if "=" in text:
            parts = text.split("=", 1)
            param.default_value = parts[1].strip()

        return param

    def _extract_block_statements(self, node: Node) -> list[StatementNode]:
        statements = []
        for child in node.children:
            if child.type in ("{", "}"):
                continue
            stmt = self._extract_statement(child)
            if stmt.kind or stmt.text:
                statements.append(stmt)
        return statements

    def _extract_statement(self, node: Node) -> StatementNode:
        return StatementNode(
            kind=node.type,
            text=_text(node),
            location=_loc(node),
        )

    def _extract_annotation(self, node: Node) -> AnnotationNode:
        name = ""
        args: dict[str, Any] = {}

        for child in node.children:
            if child.type == "identifier":
                name = _text(child)
            elif child.type == "annotation_argument_list":
                for ac in child.children:
                    args[_text(ac)] = True

        if not name:
            text = _text(node)
            m = re.match(r"@(\w+)", text)
            if m:
                name = m.group(1)

        return AnnotationNode(name=name, arguments=args, location=_loc(node))
