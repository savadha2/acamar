"""Regression test for the leaked-comment bug class (backlog item 47,
cockpit_app.py:48/489, fixed 2026-10-24 — a cockpit screenshot showed two
raw blocks of explanatory text rendered as page content).

Root cause: Streamlit's "magic" mode auto-`st.write()`s any bare top-level
string expression. A triple-quoted string placed right after a
module-level constant assignment (a PEP-258-style "attribute docstring,"
harmless in an ordinary module) is exactly that shape, and gets rendered
onto the page instead of staying a comment. Covers both dashboard
entrypoints, not just the one that actually had the bug — it's the bug
*class* being guarded against, not these two specific instances.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ENTRYPOINTS = [
    Path("src/acamar/dashboard/streamlit_app.py"),
    Path("src/acamar/dashboard/cockpit_app.py"),
]


@pytest.mark.parametrize("path", _ENTRYPOINTS, ids=lambda p: p.name)
def test_no_leaked_module_level_docstrings(path: Path) -> None:
    """The only bare top-level string expression should be the real module
    docstring (``body[0]``) — any other one is this bug, by construction.

    Deliberately narrow: this only catches bare top-level *string*
    expressions, not every possible magic-mode leak (a bare non-string
    expression, or one nested inside a function/class body) — scoped to
    the actual risk here, not built out further than that.
    """
    tree = ast.parse(path.read_text())
    leaked = [
        node.lineno
        for i, node in enumerate(tree.body)
        if i != 0
        and isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    assert leaked == [], f"{path}: bare top-level string(s) at {leaked} will render as page content"
