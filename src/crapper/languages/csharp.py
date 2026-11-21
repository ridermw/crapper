"""C# methods and cyclomatic complexity.

Scope: body-bearing `method_declaration` forms only (block or expression body).
Constructors, properties/accessors, operators, top-level statements, local
functions, and lambdas are not independent scored units; local functions and
lambdas contribute to their enclosing method.

External names remain bare (`Add`, `Box`), while each declaration retains its
own `path`, `start_line`, `end_line`, `start_byte`, and `end_byte` so overloads
and partial-class declarations remain distinct.
"""

from __future__ import annotations

from crapper.languages.treesitter import (
    child_of_type,
    complexity,
    descendants,
    end_line,
    node_text,
    parse,
    start_line,
)
from crapper.model import Function

_TYPES = {
    "class_declaration",
    "struct_declaration",
    "interface_declaration",
    "record_declaration",
    "record_struct_declaration",
}
_DECISIONS = {
    "if_statement",
    "for_statement",
    "foreach_statement",
    "while_statement",
    "do_statement",
    "catch_clause",
    "conditional_expression",
    "switch_section",
    "switch_expression_arm",
    "conditional_access_expression",
    "when_clause",
    "and_pattern",
    "or_pattern",
}


def _decision(node) -> bool:
    if node.type in _DECISIONS:
        return True
    return node.type == "binary_expression" and any(
        child.type in {"&&", "||", "??"} for child in node.children
    )


def _name(data: bytes, node) -> str:
    ident = node.child_by_field_name("name")
    if ident is not None:
        return node_text(data, ident)
    ident = child_of_type(node, "identifier")
    return node_text(data, ident) if ident is not None else ""


def _generic_arity(node) -> int:
    params = child_of_type(node, "type_parameter_list")
    if params is None:
        return 0
    return sum(1 for child in params.named_children if child.type == "type_parameter")


def _namespace_parts(data: bytes, node, root) -> list[str]:
    parts: list[str] = []
    current = node.parent
    while current is not None:
        if current.type in _TYPES:
            type_name = _name(data, current)
            if type_name:
                parts.append(type_name)
        elif current.type == "namespace_declaration":
            ns_name = _name(data, current)
            if ns_name:
                parts.append(ns_name)
        current = current.parent
    scoped = child_of_type(root, "file_scoped_namespace_declaration")
    if scoped is not None:
        ns_name = _name(data, scoped)
        if ns_name:
            parts.append(ns_name)
    return list(reversed(parts))


def _check_generic_arities(data: bytes, root, path: str) -> None:
    seen: dict[str, int] = {}
    for node in descendants(root):
        if node.type not in _TYPES:
            continue
        type_name = _name(data, node)
        if not type_name:
            continue
        parent_parts = _namespace_parts(data, node, root)
        full = ".".join([*parent_parts, type_name])
        arity = _generic_arity(node)
        previous = seen.get(full)
        if previous is not None and previous != arity:
            raise ValueError(
                f"Ambiguous generic arities for C# type {full} in {path}: "
                f"arity {previous} and {arity} share the bare namespace"
            )
        seen[full] = arity


def functions_in_source(
    source: str, path: str, project_root: str | None = None
) -> list[Function]:
    del project_root
    data, tree = parse(source, "csharp")
    if tree.root_node.has_error:
        raise ValueError(f"C# syntax could not be parsed: {path}")
    _check_generic_arities(data, tree.root_node, path)
    found: list[Function] = []
    for node in descendants(tree.root_node):
        if node.type != "method_declaration":
            continue
        body = child_of_type(node, "block", "arrow_expression_clause")
        if body is None:
            continue
        name = _name(data, node)
        if not name:
            continue
        parts = _namespace_parts(data, node, tree.root_node)
        namespace = ".".join(parts)
        found.append(
            Function(
                name=name,
                namespace=namespace,
                complexity=complexity(body, _decision, lambda n: n.type in _TYPES),
                start_line=start_line(node),
                end_line=end_line(node),
                path=path,
                language="csharp",
                start_byte=node.start_byte,
                end_byte=node.end_byte,
            )
        )
    return found
