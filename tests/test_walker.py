"""Tests for scope building, import map, and call edge extraction."""
import ast
import pytest

from vizzpy.parser.walker import build_scope, build_import_map, CallVisitor


# ── ScopeBuilder ──────────────────────────────────────────────────────────────

def _scope(src, module="m"):
    return build_scope(module, ast.parse(src))


def test_top_level_functions():
    s = _scope("def foo(): pass\ndef bar(): pass")
    assert {"m.foo", "m.bar"} <= s.names


def test_class_method():
    s = _scope("class A:\n    def method(self): pass")
    assert "m.A.method" in s.names


def test_init_uses_class_name():
    s = _scope("class A:\n    def __init__(self): pass")
    assert "m.A" in s.names
    assert "m.A.__init__" not in s.names


def test_init_display_name_is_class():
    s = _scope("class A:\n    def __init__(self): pass")
    span = next(sp for sp in s.spans if sp.qualified_name == "m.A")
    assert span.display_name == "A"


def test_method_display_name():
    s = _scope("class B:\n    def process(self): pass")
    span = next(sp for sp in s.spans if sp.qualified_name == "m.B.process")
    assert span.display_name == "B.process"


def test_nested_function_not_registered():
    s = _scope("def outer():\n    def inner(): pass")
    assert "m.outer" in s.names
    assert "m.inner" not in s.names


def test_docstring_captured():
    s = _scope('def foo():\n    """My docstring."""\n    pass')
    span = next(sp for sp in s.spans if sp.qualified_name == "m.foo")
    assert span.docstring == "My docstring."


def test_async_function():
    s = _scope("async def fetch(): pass")
    assert "m.fetch" in s.names


# ── build_import_map ──────────────────────────────────────────────────────────

def _imap(src, module="cli"):
    return build_import_map(ast.parse(src), module)


def test_from_import():
    m = _imap("from services.order import OrderService")
    assert m["OrderService"] == "services.order.OrderService"


def test_from_import_alias():
    m = _imap("from services.order import OrderService as OS")
    assert m["OS"] == "services.order.OrderService"


def test_bare_import():
    m = _imap("import os")
    assert m["os"] == "os"


def test_relative_import():
    m = _imap("from . import utils", module="pkg.module")
    assert m["utils"] == "pkg.utils"


def test_relative_import_nested():
    m = _imap("from .helpers import helper", module="pkg.sub.module")
    assert m["helper"] == "pkg.sub.helpers.helper"


def test_star_import_skipped():
    m = _imap("from os.path import *")
    assert not any(k.startswith("*") for k in m)


# ── CallVisitor ───────────────────────────────────────────────────────────────

def _edges(src, module="m", extra_names=None, suffix_index=None):
    tree = ast.parse(src)
    scope = build_scope(module, tree)
    imap  = build_import_map(tree, module)
    all_names = scope.names | (extra_names or set())
    v = CallVisitor(module, imap, all_names, suffix_index)
    v.visit(tree)
    return v.edges


def test_direct_call():
    edges = _edges("def a(): pass\ndef b():\n    a()")
    assert ("m.b", "m.a") in edges


def test_self_method_call():
    edges = _edges("class A:\n    def foo(self): pass\n    def bar(self):\n        self.foo()")
    assert ("m.A.bar", "m.A.foo") in edges


def test_no_self_recursion():
    edges = _edges("def f():\n    f()")
    assert ("m.f", "m.f") not in edges


def test_class_instantiation():
    src = "class Worker:\n    def __init__(self): pass\ndef run():\n    Worker()"
    edges = _edges(src)
    # Worker() is a call to __init__, which maps to the class node "m.Worker"
    assert ("m.run", "m.Worker") in edges


def test_imported_call_resolved():
    src = "from utils import helper\ndef caller():\n    helper()"
    edges = _edges(src, extra_names={"utils.helper"})
    assert ("m.caller", "utils.helper") in edges


def test_suffix_resolution_cross_package():
    """Import from 'other_pkg' but the function lives in 'our_pkg' — suffix match."""
    src = "from other_pkg.utils import helper\ndef caller():\n    helper()"
    extra = {"our_pkg.utils.helper"}
    sindex = {"utils.helper": "our_pkg.utils.helper", "helper": "our_pkg.utils.helper"}
    edges = _edges(src, extra_names=extra, suffix_index=sindex)
    assert ("m.caller", "our_pkg.utils.helper") in edges


def test_cls_method_call():
    src = "class A:\n    def create(cls):\n        pass\n    def run(self):\n        A.create()"
    edges = _edges(src)
    assert ("m.A.run", "m.A.create") in edges


# ── Additional CallVisitor coverage ──────────────────────────────────────────

def test_nested_function_call_attributed_to_parent():
    # Calls inside a nested function should be attributed to the enclosing function.
    src = (
        "def outer():\n"
        "    def inner(): pass\n"
        "    inner()\n"
        "def helper(): pass\n"
        "def top():\n"
        "    def nested():\n"
        "        helper()\n"
        "    nested()\n"
    )
    edges = _edges(src)
    # Call to helper() inside nested() is attributed to top() (the registered parent)
    assert ("m.top", "m.helper") in edges


def test_async_function_call():
    src = "async def fetch(): pass\nasync def run():\n    await fetch()"
    # fetch() is called inside run() — edges should capture it
    edges = _edges(src)
    assert ("m.run", "m.fetch") in edges


def test_self_init_call():
    # self.__init__() should resolve to the class node (same as Worker())
    src = "class A:\n    def __init__(self): pass\n    def reset(self):\n        self.__init__()"
    edges = _edges(src)
    assert ("m.A.reset", "m.A") in edges


def test_cls_attr_no_class_context():
    # cls.method() outside a class should produce no edge
    src = "def standalone():\n    cls = None"
    edges = _edges(src)
    assert edges == []


def test_resolve_call_non_name_non_attr():
    # Calling a subscript result: arr[0]() — should not crash, produces no edge
    src = "def f(): pass\ndef g():\n    funcs = [f]\n    funcs[0]()"
    edges = _edges(src)
    # No edge for funcs[0]() since it's not a Name or Attribute node
    assert ("m.g", "m.f") not in edges


def test_dotted_import_alias_call():
    # import pkg.sub; pkg.sub.func()
    src = "import pkg.sub\ndef caller():\n    pkg.sub.func()"
    edges = _edges(src, extra_names={"pkg.sub.func"})
    assert ("m.caller", "pkg.sub.func") in edges


def test_chained_attr_call_suffix_fallback():
    # a.b.helper() — deep attribute chain resolved via suffix index
    src = "def caller():\n    a.b.helper()"
    extra = {"pkg.b.helper"}
    sindex = {"b.helper": "pkg.b.helper", "helper": "pkg.b.helper"}
    edges = _edges(src, extra_names=extra, suffix_index=sindex)
    assert ("m.caller", "pkg.b.helper") in edges


def test_module_level_call():
    # Call at module level (outside any function) → uses __main__ as synthetic caller
    src = "def setup(): pass\nsetup()"
    edges = _edges(src)
    # module-level calls are attributed to m.__main__
    assert any(e[0] == "m.__main__" and e[1] == "m.setup" for e in edges)


# ── External call resolution ──────────────────────────────────────────────────

def test_external_call_via_import_alias():
    # import pandas as pd; pd.read_csv() → edge to pandas.read_csv (external leaf)
    src = "import pandas as pd\ndef load():\n    pd.read_csv('f.csv')"
    edges = _edges(src)
    assert ("m.load", "pandas.read_csv") in edges


def test_external_call_via_from_import():
    # from csv import reader; reader() → edge to csv.reader (external leaf)
    src = "from csv import reader\ndef parse():\n    reader(open('f'))"
    edges = _edges(src)
    assert ("m.parse", "csv.reader") in edges


def test_external_call_direct_module_attr():
    # import os; os.getcwd() → edge to os.getcwd (external leaf)
    src = "import os\ndef run():\n    os.getcwd()"
    edges = _edges(src)
    assert ("m.run", "os.getcwd") in edges


def test_unimported_builtin_not_included():
    # print() is not in import_map and not a project function → no edge
    src = "def f():\n    print('hello')"
    edges = _edges(src)
    assert not any("print" in e[1] for e in edges)


def test_external_does_not_shadow_project_function(tmp_path):
    # If obj_name maps via import_map but a local class has the same name, prefer local
    src = (
        "from other import Foo\n"
        "class Foo:\n"
        "    def bar(self): pass\n"
        "def caller():\n"
        "    Foo.bar()\n"
    )
    edges = _edges(src)
    assert ("m.caller", "m.Foo.bar") in edges
    assert not any(e[1] == "other.Foo.bar" for e in edges)
