"""
AST visitors for building function scopes and extracting call edges.
"""
from __future__ import annotations
import ast
from typing import Optional

from .scope import FuncScope, FuncSpan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _qualified_name(module: str, class_stack: list[str], func_name: str) -> str:
    if class_stack:
        class_name = class_stack[-1]
        if func_name == "__init__":
            return f"{module}.{class_name}"
        return f"{module}.{class_name}.{func_name}"
    return f"{module}.{func_name}"


def _display_name(class_stack: list[str], func_name: str) -> str:
    if class_stack:
        class_name = class_stack[-1]
        if func_name == "__init__":
            return class_name
        return f"{class_name}.{func_name}"
    return func_name


# ---------------------------------------------------------------------------
# Scope builder: first pass — collect all function/method definitions
# ---------------------------------------------------------------------------

class ScopeBuilder(ast.NodeVisitor):
    """Collects FuncSpan entries for every top-level function and class method."""

    def __init__(self, module_name: str):
        self.scope = FuncScope(module_name)
        self._module = module_name
        self._class_stack: list[str] = []
        self._in_func = False  # True once inside any function body

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def _visit_funcdef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Only register top-level functions and direct class methods, not nested functions.
        if not self._in_func:
            qname = _qualified_name(self._module, self._class_stack, node.name)
            dname = _display_name(self._class_stack, node.name)
            self.scope.add(FuncSpan(
                qualified_name=qname,
                display_name=dname,
                module_name=self._module,
                start=node.lineno,
                end=node.end_lineno,
                docstring=ast.get_docstring(node),
            ))
        prev = self._in_func
        self._in_func = True
        self.generic_visit(node)
        self._in_func = prev

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_funcdef(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_funcdef(node)


def build_scope(module_name: str, tree: ast.AST) -> FuncScope:
    builder = ScopeBuilder(module_name)
    builder.visit(tree)
    return builder.scope


# ---------------------------------------------------------------------------
# Import resolution: build a map from local alias → qualified name
# ---------------------------------------------------------------------------

def build_import_map(tree: ast.AST, module_name: str) -> dict[str, str]:
    """
    Returns {local_name: qualified_name} for all imports in the file.

    Examples:
      from services.order import OrderService       -> {'OrderService': 'services.order.OrderService'}
      from services.order import OrderService as OS -> {'OS': 'services.order.OrderService'}
      import services.order                         -> {'services': 'services', 'services.order': 'services.order'}
    """
    import_map: dict[str, str] = {}
    module_parts = module_name.split(".")

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.names and node.names[0].name == "*":
                continue  # skip star imports — can't resolve statically

            mod = node.module or ""

            # Resolve relative imports
            if node.level > 0:
                base = module_parts[:max(0, len(module_parts) - node.level)]
                mod = ".".join(base + ([mod] if mod else []))

            for alias in node.names:
                local = alias.asname if alias.asname else alias.name
                import_map[local] = f"{mod}.{alias.name}" if mod else alias.name

        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname if alias.asname else alias.name
                import_map[local] = alias.name
                # Also register dotted prefix so `services.order.func` resolves
                if "." in alias.name:
                    parts = alias.name.split(".")
                    for i in range(1, len(parts) + 1):
                        prefix = ".".join(parts[:i])
                        import_map.setdefault(prefix, prefix)

    return import_map


# ---------------------------------------------------------------------------
# Call visitor: second pass — extract caller → callee edges
# ---------------------------------------------------------------------------

class CallVisitor(ast.NodeVisitor):
    """
    Walks the AST of a single module and emits (caller_qname, callee_qname) edges.
    Only edges where the callee is in *all_names* (the project-wide set of known
    function qualified names) are recorded.
    """

    def __init__(
        self,
        module_name: str,
        import_map: dict[str, str],
        all_names: set[str],
        suffix_index: dict[str, "str | None"] | None = None,
    ):
        self._module = module_name
        self._import_map = import_map
        self._all_names = all_names
        self._suffix_index: dict[str, str | None] = suffix_index or {}
        self.edges: list[tuple[str, str]] = []

        self._class_stack: list[str] = []
        # Stack of qualified names representing the current call chain.
        # Nested functions fall back to their parent's qname as the caller.
        self._func_stack: list[str] = []

    # -- context tracking ----------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def _visit_funcdef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Determine the qualified name to use as caller context.
        # For nested functions, reuse the enclosing registered function's qname.
        if not self._func_stack:
            qname = _qualified_name(self._module, self._class_stack, node.name)
        else:
            qname = self._func_stack[-1]  # nested → attribute calls still emit from parent

        self._func_stack.append(qname)
        self.generic_visit(node)
        self._func_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_funcdef(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_funcdef(node)

    # -- call extraction -----------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        caller = self._func_stack[-1] if self._func_stack else f"{self._module}.__main__"
        callee = self._resolve_call(node)
        if callee and callee != caller:
            self.edges.append((caller, callee))
        self.generic_visit(node)

    def _resolve_call(self, node: ast.Call) -> Optional[str]:
        if isinstance(node.func, ast.Name):
            return self._resolve_name(node.func.id)
        if isinstance(node.func, ast.Attribute):
            return self._resolve_attr(node.func)
        return None

    def _resolve_name(self, name: str) -> Optional[str]:
        # Check import map first
        if name in self._import_map:
            candidate = self._import_map[name]
            if candidate in self._all_names:
                return candidate
            # Import found but qname unknown — try suffix matching to handle
            # package renames (e.g. folder is "pkg_v2" but code imports "pkg")
            result = self._resolve_via_suffix(candidate)
            if result:
                return result
            # Resolved via import but not a project function — treat as external leaf
            return candidate
        # Try as a function in this module
        qname = f"{self._module}.{name}"
        return qname if qname in self._all_names else None

    def _resolve_via_suffix(self, candidate: str) -> Optional[str]:
        """
        Try progressively shorter suffixes of *candidate* against the suffix index.
        Returns the unambiguous match, or None if not found / ambiguous.
        """
        parts = candidate.split(".")
        for i in range(1, len(parts)):
            suffix = ".".join(parts[i:])
            if suffix in self._suffix_index:
                return self._suffix_index[suffix]  # None if ambiguous
        return None

    def _resolve_attr(self, node: ast.Attribute) -> Optional[str]:
        attr = node.attr

        # self.method()
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            return self._resolve_self_attr(attr)

        # cls.method()
        if isinstance(node.value, ast.Name) and node.value.id == "cls":
            return self._resolve_cls_attr(attr)

        # SomeName.method() — could be ClassName.method, module.func, or imported.func
        if isinstance(node.value, ast.Name):
            return self._resolve_dotted(node.value.id, attr)

        # module.submodule.func() — e.g. ast.Attribute chain
        if isinstance(node.value, ast.Attribute):
            # Reconstruct the dotted prefix as best we can
            prefix = self._unparse_attr_chain(node.value)
            if prefix:
                qname = f"{prefix}.{attr}"
                if qname in self._all_names:
                    return qname
                # Maybe prefix is an import alias
                if prefix in self._import_map:
                    resolved = f"{self._import_map[prefix]}.{attr}"
                    if resolved in self._all_names:
                        return resolved
                    # External call via chained import
                    return resolved
                # Suffix fallback for chained attribute calls
                result = self._resolve_via_suffix(qname)
                if result:
                    return result

        return None

    def _resolve_self_attr(self, attr: str) -> Optional[str]:
        if not self._class_stack:
            return None
        class_name = self._class_stack[-1]
        if attr == "__init__":
            qname = f"{self._module}.{class_name}"
        else:
            qname = f"{self._module}.{class_name}.{attr}"
        return qname if qname in self._all_names else None

    def _resolve_cls_attr(self, attr: str) -> Optional[str]:
        if not self._class_stack:
            return None
        class_name = self._class_stack[-1]
        qname = f"{self._module}.{class_name}.{attr}"
        return qname if qname in self._all_names else None

    def _resolve_dotted(self, obj_name: str, attr: str) -> Optional[str]:
        # obj is an import alias pointing to a module or class
        if obj_name in self._import_map:
            base = self._import_map[obj_name]
            qname = f"{base}.{attr}"
            if qname in self._all_names:
                return qname
            result = self._resolve_via_suffix(qname)
            if result:
                return result
            # Prefer local class definition with the same name if it exists
            local_qname = f"{self._module}.{obj_name}.{attr}"
            if local_qname in self._all_names:
                return local_qname
            # Import alias found but not a project function — treat as external leaf
            return qname
        # obj is a class in this module (ClassName.method or ClassName() == __init__)
        qname = f"{self._module}.{obj_name}.{attr}"
        if qname in self._all_names:
            return qname
        return None

    def _unparse_attr_chain(self, node: ast.expr) -> Optional[str]:
        """Reconstruct a dotted name from a chain of ast.Attribute nodes."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = self._unparse_attr_chain(node.value)
            return f"{prefix}.{node.attr}" if prefix else None
        return None
