"""PowerShell functions, class methods, and cyclomatic complexity.

Paths are normalized against `project_root` before deriving module namespaces,
so relative crapper paths and absolute mutator paths always produce the same
canonical namespace. One leading `src/` segment is stripped; other directories
such as `scripts/` stay part of the namespace.
"""

from __future__ import annotations

from pathlib import Path
import sys

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

_DECISIONS = {
    "if_statement",
    "elseif_clause",
    "switch_clause",
    "for_statement",
    "foreach_statement",
    "while_statement",
    "do_statement",
    "catch_clause",
    "trap_statement",
}
_SCOPES = ("global:", "local:", "script:", "private:")


def _module_namespace(path: str, project_root: str | None) -> str:
    file_path = Path(path)
    relative: str
    if project_root:
        root = Path(project_root).resolve()
        candidate = file_path.resolve() if file_path.is_absolute() else (root / file_path).resolve()
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            relative = file_path.as_posix().lstrip("./")
    else:
        relative = file_path.as_posix().replace("\\", "/").lstrip("./")
    if relative.startswith("src/"):
        relative = relative[4:]
    for suffix in (".psm1", ".ps1"):
        if relative.lower().endswith(suffix):
            relative = relative[: -len(suffix)]
            break
    return relative.replace("/", ".")


def _strip_scope(name: str) -> str:
    lower = name.lower()
    for prefix in _SCOPES:
        if lower.startswith(prefix):
            return name[len(prefix) :]
    return name


def _is_nested_function(node) -> bool:
    current = node.parent
    while current is not None:
        if current.type in {"function_statement", "class_method_definition"}:
            return True
        current = current.parent
    return False


def _class_name(data: bytes, node) -> str:
    current = node.parent
    while current is not None:
        if current.type == "class_statement":
            ident = child_of_type(current, "simple_name")
            if ident is not None:
                return node_text(data, ident)
        current = current.parent
    return ""


def _decision(data: bytes):
    def check(node) -> bool:
        if node.type in _DECISIONS:
            return True
        if not node.is_named:
            low = node_text(data, node).strip().lower()
            if low in {"-and", "-or", "&&", "||"}:
                return True
        return False

    return check


def functions_in_source(
    source: str, path: str, project_root: str | None = None
) -> list[Function]:
    data, tree = parse(source, "powershell")
    module = _module_namespace(path, project_root)
    found: list[Function] = []
    skipped_errors = 0
    decision_fn = _decision(data)

    for node in descendants(tree.root_node):
        if node.type == "function_statement":
            if _is_nested_function(node):
                continue
            if node.has_error:
                skipped_errors += 1
                continue
            name_node = child_of_type(node, "function_name")
            body = child_of_type(node, "script_block")
            if name_node is None or body is None:
                continue
            name = _strip_scope(node_text(data, name_node).strip())
            found.append(
                Function(
                    name=name,
                    namespace=module,
                    complexity=complexity(body, decision_fn, lambda n: n.type == "class_statement"),
                    start_line=start_line(node),
                    end_line=end_line(node),
                    path=path,
                    language="powershell",
                    start_byte=node.start_byte,
                    end_byte=node.end_byte,
                )
            )
        elif node.type == "class_method_definition":
            if node.has_error:
                skipped_errors += 1
                continue
            ident = child_of_type(node, "simple_name")
            body = child_of_type(node, "script_block")
            if ident is None or body is None:
                continue
            cls = _class_name(data, node)
            namespace = ".".join([p for p in (module, cls) if p])
            found.append(
                Function(
                    name=node_text(data, ident).strip(),
                    namespace=namespace,
                    complexity=complexity(body, decision_fn, lambda _: False),
                    start_line=start_line(node),
                    end_line=end_line(node),
                    path=path,
                    language="powershell",
                    start_byte=node.start_byte,
                    end_byte=node.end_byte,
                )
            )

    if skipped_errors:
        print(
            f"crapper: skipped {skipped_errors} malformed PowerShell form(s) in {path}",
            file=sys.stderr,
        )
    return found
