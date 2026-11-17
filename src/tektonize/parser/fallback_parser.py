"""Regex-based fallback parser for Groovy files that tree-sitter can't fully handle.

This handles common Jenkins shared-library patterns where the
tree-sitter-groovy grammar produces ERROR nodes: implements clauses, complex
field initializers, Groovy-specific operators, etc.
"""

from __future__ import annotations

import re

from tektonize.parser.ast_nodes import (
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
    TypeRef,
)


def _loc_from_line(line_num: int) -> SourceLocation:
    return SourceLocation(start_line=line_num, end_line=line_num)


def parse_groovy_fallback(source: str, file_path: str = "<string>") -> GroovySourceFile:
    """Parse a Groovy file using regex patterns when tree-sitter fails."""
    result = GroovySourceFile(file_path=file_path)
    lines = source.split("\n")

    # Extract package
    for line in lines:
        m = re.match(r"^\s*package\s+([\w.]+)", line)
        if m:
            result.package = m.group(1)
            break

    # Extract imports
    for i, line in enumerate(lines):
        m = re.match(r"^\s*import\s+(static\s+)?([\w.*]+)(?:\s+as\s+(\w+))?\s*$", line)
        if m:
            result.imports.append(
                ImportDecl(
                    path=m.group(2),
                    alias=m.group(3),
                    is_static=bool(m.group(1)),
                    is_wildcard=m.group(2).endswith("*"),
                    location=_loc_from_line(i + 1),
                )
            )

    # Find class/interface/enum declarations
    _extract_declarations(source, lines, result)

    return result


def _extract_declarations(source: str, lines: list[str], result: GroovySourceFile) -> None:
    """Extract class, interface, and enum declarations from source."""
    # Pattern for class/interface/enum headers
    decl_pattern = re.compile(
        r"^(?P<modifiers>(?:(?:public|private|protected|abstract|static|final)\s+)*)"
        r"(?P<kind>class|interface|enum)\s+"
        r"(?P<name>\w+)"
        r"(?:\s+extends\s+(?P<extends>[\w.]+))?"
        r"(?:\s+implements\s+(?P<implements>[\w.,\s]+))?"
        r"\s*\{",
        re.MULTILINE,
    )

    for match in decl_pattern.finditer(source):
        kind_str = match.group("kind")
        name = match.group("name")
        modifiers_str = match.group("modifiers").strip()
        modifiers = modifiers_str.split() if modifiers_str else []
        extends = match.group("extends")
        implements_str = match.group("implements")
        implements = (
            [i.strip() for i in implements_str.split(",") if i.strip()] if implements_str else []
        )

        # Find the matching closing brace
        body_start = match.end() - 1
        body = _extract_brace_block(source, body_start)

        # Get preceding javadoc
        line_num = source[: match.start()].count("\n") + 1
        javadoc = _get_preceding_javadoc(lines, line_num)

        if kind_str == "enum":
            enum = EnumDecl(
                kind=NodeKind.ENUM,
                name=name,
                package=result.package,
                modifiers=modifiers,
                implements=implements,
                values=_extract_enum_values(body),
                methods=_extract_methods(body, name),
                fields=_extract_fields(body),
                javadoc=javadoc,
                location=_loc_from_line(line_num),
            )
            result.classes.append(enum)

        elif kind_str == "interface":
            iface = InterfaceDecl(
                kind=NodeKind.INTERFACE,
                name=name,
                package=result.package,
                modifiers=modifiers,
                extends=[extends] if extends else [],
                fields=_extract_fields(body),
                methods=_extract_methods(body, name),
                javadoc=javadoc,
                location=_loc_from_line(line_num),
            )
            result.classes.append(iface)

        else:
            cls = ClassDecl(
                kind=NodeKind.CLASS,
                name=name,
                package=result.package,
                modifiers=modifiers,
                extends=extends,
                implements=implements,
                fields=_extract_fields(body),
                methods=_extract_methods(body, name),
                constructors=_extract_constructors(body, name),
                javadoc=javadoc,
                location=_loc_from_line(line_num),
            )
            result.classes.append(cls)

    # Check for vars/ style top-level methods (def call(...))
    if not result.classes:
        _extract_top_level_methods(source, lines, result)


def _extract_brace_block(source: str, start: int) -> str:
    """Extract content between matching braces starting at position start."""
    if start >= len(source) or source[start] != "{":
        return ""
    depth = 0
    i = start
    in_string = False
    string_char = ""
    while i < len(source):
        ch = source[i]
        if in_string:
            if ch == "\\" and i + 1 < len(source):
                i += 2
                continue
            if ch == string_char:
                in_string = False
        else:
            if ch in ('"', "'"):
                in_string = True
                string_char = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return source[start + 1 : i]
        i += 1
    return source[start + 1 :]


def _extract_enum_values(body: str) -> list[EnumValue]:
    """Extract enum constants from an enum body."""
    values = []
    lines = body.split("\n")

    prev_javadoc = None
    for line in lines:
        stripped = line.strip()

        # Javadoc comment
        if stripped.startswith("/**"):
            prev_javadoc = stripped
            continue
        if stripped.startswith("*"):
            if prev_javadoc:
                prev_javadoc += "\n" + stripped
            continue
        if stripped.endswith("*/"):
            if prev_javadoc:
                prev_javadoc += "\n" + stripped
            continue

        # Enum constant: UPPER_CASE or UPPER_CASE(args), optional trailing , or ;
        m = re.match(
            r"^\s*([A-Z][A-Z0-9_]*)\s*(?:\(([^)]*)\))?\s*[,;]?\s*$",
            stripped,
        )
        if m:
            raw_args = m.group(2)
            if raw_args is None or not raw_args.strip():
                args: list[str] = []
            else:
                args = [a.strip() for a in raw_args.split(",") if a.strip()]
            values.append(
                EnumValue(
                    name=m.group(1),
                    arguments=args,
                    javadoc=prev_javadoc,
                )
            )
            prev_javadoc = None
            continue

        # Stop at the first method or field that isn't an enum constant
        if re.match(
            r"^\s*(static\s+|def\s+|void\s+|public\s+|private\s+|protected\s+|final\s+)",
            stripped,
        ):
            break

    return values


def _extract_fields(body: str) -> list[FieldDecl]:
    """Extract field declarations from a class body.

    Only extracts lines that appear BEFORE the first method declaration,
    to avoid misinterpreting method body lines as fields.
    """
    fields = []
    lines = body.split("\n")

    prev_javadoc = None
    in_method = False
    brace_depth = 0

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Track brace depth to know if we're inside a method
        brace_depth += stripped.count("{") - stripped.count("}")
        if brace_depth < 0:
            brace_depth = 0

        # Track javadoc
        if stripped.startswith("/**"):
            prev_javadoc = stripped
            continue
        if prev_javadoc and (stripped.startswith("*") or stripped.endswith("*/")):
            prev_javadoc += "\n" + stripped
            continue

        # Detect method/constructor start — stop extracting fields once inside
        if re.match(
            r"^\s*(?:(?:public|private|protected|static|abstract|final|def|synchronized)\s+)*"
            r"(?:[\w<>,.\[\]]+\s+)?\w+\s*\([^)]*\)\s*\{",
            stripped,
        ):
            in_method = True
            prev_javadoc = None
            continue

        if in_method:
            prev_javadoc = None
            continue

        # Skip if it looks like a method call or has parentheses
        if "(" in stripped and not stripped.startswith("//"):
            prev_javadoc = None
            continue

        # Field pattern: [modifiers] Type name [= value]
        m = re.match(
            r"^(?:(public|private|protected|static|final|transient|volatile)\s+)*"
            r"([\w<>,.\s\[\]]+?)\s+"
            r"(\w+)\s*"
            r"(?:=\s*(.+?))?\s*$",
            stripped,
        )
        if m:
            type_text = m.group(2).strip()
            name = m.group(3)
            default_val = m.group(4)

            # Skip if type looks like a control keyword
            if type_text in ("def", "void", "return", "if", "for", "while", "throw", "try"):
                prev_javadoc = None
                continue

            # Skip if name is a keyword
            if name in ("return", "if", "for", "while", "throw", "new", "try"):
                prev_javadoc = None
                continue

            modifiers = []
            for mod in ("public", "private", "protected", "static", "final"):
                if re.search(rf"\b{mod}\b", stripped):
                    modifiers.append(mod)

            fields.append(
                FieldDecl(
                    name=name,
                    type_ref=TypeRef(name=type_text) if type_text not in ("def",) else None,
                    modifiers=modifiers,
                    default_value=default_val,
                    javadoc=prev_javadoc,
                    location=_loc_from_line(i + 1),
                )
            )
            prev_javadoc = None
        elif stripped and not stripped.startswith("//") and not stripped.startswith("/*"):
            prev_javadoc = None

    return fields


def _extract_methods(body: str, class_name: str = "") -> list[MethodDecl]:
    """Extract method declarations from a class/interface body."""
    methods = []

    # Require a return type/modifier and an opening brace so call sites like
    # ``ad.getOrDefault('x', false)`` or ``script.stage('X') {`` are not treated
    # as method declarations.
    method_pattern = re.compile(
        r"(?P<modifiers>(?:(?:public|private|protected|static|abstract|final|def|synchronized)\s+)*)"
        r"(?P<return_type>[\w<>,.\[\]]+)\s+"
        r"(?P<name>\w+)\s*"
        r"\((?P<params>[^)]*)\)\s*"
        r"\{",
        re.MULTILINE,
    )

    for match in method_pattern.finditer(body):
        name = match.group("name")

        # Skip constructor (handled separately) and keywords
        if name == class_name:
            continue
        if name in ("if", "for", "while", "switch", "catch", "try", "return", "new", "throw"):
            continue

        # Method calls: ``obj.method(...) {`` / ``.method(...)``
        if match.start() > 0 and body[match.start() - 1] == ".":
            continue

        modifiers_str = (match.group("modifiers") or "").strip()
        modifiers = [m for m in modifiers_str.split() if m not in ("def",)]
        return_type_str = (match.group("return_type") or "").strip()
        if return_type_str in (
            "if",
            "for",
            "while",
            "switch",
            "catch",
            "try",
            "return",
            "new",
            "throw",
            "else",
        ):
            continue
        params_str = match.group("params")

        # Reject call-like argument lists (string/boolean literals as "params")
        if _params_look_like_call_args(params_str):
            continue

        body_start = body.find("{", match.end() - 1)
        method_body = ""
        if body_start >= 0 and body_start < match.end() + 5:
            method_body = _extract_brace_block(body, body_start)

        methods.append(
            MethodDecl(
                name=name,
                return_type=TypeRef(name=return_type_str) if return_type_str else None,
                parameters=_parse_param_list(params_str),
                modifiers=modifiers,
                body_text=method_body,
            )
        )

    # Interface / abstract declarations without a body: ``void foo(Type x)``
    abstract_pattern = re.compile(
        r"(?P<modifiers>(?:(?:public|private|protected|static|abstract|final|def|synchronized)\s+)*)"
        r"(?P<return_type>[\w<>,.\[\]]+)\s+"
        r"(?P<name>\w+)\s*"
        r"\((?P<params>[^)]*)\)\s*"
        r"(?:;|(?=\n))",
        re.MULTILINE,
    )
    seen = {m.name for m in methods}
    for match in abstract_pattern.finditer(body):
        name = match.group("name")
        if name in seen or name == class_name:
            continue
        if name in ("if", "for", "while", "switch", "catch", "try", "return", "new", "throw"):
            continue
        if match.start() > 0 and body[match.start() - 1] == ".":
            continue
        # Skip if this match is actually the header of a braced method
        after = body[match.end() : match.end() + 8].lstrip()
        if after.startswith("{"):
            continue
        return_type_str = (match.group("return_type") or "").strip()
        if return_type_str in (
            "if",
            "for",
            "while",
            "switch",
            "catch",
            "try",
            "return",
            "new",
            "throw",
            "else",
        ):
            continue
        params_str = match.group("params")
        if _params_look_like_call_args(params_str):
            continue
        modifiers_str = (match.group("modifiers") or "").strip()
        modifiers = [m for m in modifiers_str.split() if m not in ("def",)]
        methods.append(
            MethodDecl(
                name=name,
                return_type=TypeRef(name=return_type_str) if return_type_str else None,
                parameters=_parse_param_list(params_str),
                modifiers=modifiers,
                body_text="",
            )
        )
        seen.add(name)

    return methods


_GROOVY_PARAM_TYPES = frozenset(
    {
        "def",
        "void",
        "int",
        "long",
        "boolean",
        "byte",
        "short",
        "float",
        "double",
        "char",
    }
)


def _params_look_like_call_args(params_str: str) -> bool:
    """Return True when a parameter list looks like call arguments, not formals."""
    stripped = params_str.strip()
    if not stripped:
        return False
    for part in _split_params(stripped):
        part = part.strip()
        if not part:
            continue
        if part[0] in "'\"" or part in ("true", "false", "null"):
            return True
        if part.startswith("[") or part.startswith(":"):
            return True
        # Formal params are ``Type name`` or ``name``; bare literals are calls.
        if re.fullmatch(r"\d+(\.\d+)?", part):
            return True
        m = re.match(r"^(?:([\w<>,.\[\]]+)\s+)?(\w+)(?:\s*=\s*(.+))?$", part)
        if not m:
            return True
        type_str = m.group(1)
        # Reject comment prose like ``if any`` / ``if configured`` parsed as Type name
        if type_str and type_str[0].islower() and type_str not in _GROOVY_PARAM_TYPES:
            return True
    return False


def _extract_constructors(body: str, class_name: str) -> list[MethodDecl]:
    """Extract constructor declarations from a class body."""
    constructors = []

    ctor_pattern = re.compile(
        rf"(?:(?P<modifiers>(?:public|private|protected)\s+)?)"
        rf"(?P<name>{re.escape(class_name)})\s*"
        rf"\((?P<params>[^)]*)\)\s*\{{",
        re.MULTILINE,
    )

    for match in ctor_pattern.finditer(body):
        params_str = match.group("params")
        body_start = body.find("{", match.end() - 1)
        ctor_body = ""
        if body_start >= 0:
            ctor_body = _extract_brace_block(body, body_start)

        constructors.append(
            MethodDecl(
                name=class_name,
                parameters=_parse_param_list(params_str),
                body_text=ctor_body,
                is_constructor=True,
            )
        )

    return constructors


def _extract_top_level_methods(source: str, lines: list[str], result: GroovySourceFile) -> None:
    """Extract top-level method declarations (e.g. vars/ call() methods)."""
    method_pattern = re.compile(
        r"^(?P<return_type>def\s+)?(?P<name>call|\w+)\s*\((?P<params>[^)]*)\)\s*\{",
        re.MULTILINE,
    )

    for match in method_pattern.finditer(source):
        name = match.group("name")
        if name in ("package", "import", "if", "for", "while", "class", "interface", "enum"):
            continue

        params_str = match.group("params")
        body_start = source.find("{", match.end() - 1)
        method_body = ""
        if body_start >= 0:
            method_body = _extract_brace_block(source, body_start)

        result.top_level_methods.append(
            MethodDecl(
                name=name,
                parameters=_parse_param_list(params_str),
                body_text=method_body,
                is_constructor=False,
            )
        )


def _parse_param_list(params_str: str) -> list[ParameterNode]:
    """Parse a parameter list string into ParameterNode objects."""
    params: list[ParameterNode] = []
    if not params_str.strip():
        return params

    # Split on commas, respecting angle brackets and nested generics
    parts = _split_params(params_str)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Match: [Type] name [= default]
        m = re.match(
            r"^(?:(\w[\w<>,.\[\]]*)\s+)?(\w+)\s*(?:=\s*(.+))?$",
            part.strip(),
        )
        if m:
            type_str = m.group(1)
            name = m.group(2)
            default = m.group(3)
            params.append(
                ParameterNode(
                    name=name,
                    type_ref=TypeRef(name=type_str) if type_str else None,
                    default_value=default.strip() if default else None,
                )
            )
        else:
            # Fall back: just use the text as name
            params.append(ParameterNode(name=part))

    return params


def _split_params(text: str) -> list[str]:
    """Split parameter list on commas, respecting nested angle brackets."""
    parts = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch in ("<", "(", "["):
            depth += 1
        elif ch in (">", ")", "]"):
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def _get_preceding_javadoc(lines: list[str], decl_line: int) -> str | None:
    """Get javadoc comment preceding a declaration."""
    # Walk backwards from the declaration line
    idx = decl_line - 2  # 0-indexed, one before declaration
    if idx < 0:
        return None

    # Find end of javadoc (*/)
    while idx >= 0 and not lines[idx].strip():
        idx -= 1

    if idx < 0 or "*/" not in lines[idx]:
        return None

    # Find start of javadoc (/**)
    end_idx = idx
    while idx >= 0:
        if "/**" in lines[idx]:
            return "\n".join(lines[idx : end_idx + 1])
        idx -= 1

    return None
