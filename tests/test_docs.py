"""Run every python code block in README.md and docs/*.md so the docs cannot rot."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
FILES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
FENCE = re.compile(r"^```python\n(.*?)^```", re.S | re.M)
BLOCKS = [pytest.param(code, id=f"{f.name}#{i}")
          for f in FILES for i, code in enumerate(FENCE.findall(f.read_text()))]


def test_docs_have_runnable_examples():
    assert len(BLOCKS) >= 12


@pytest.mark.parametrize("code", BLOCKS)
def test_doc_snippet_runs(code, monkeypatch):
    monkeypatch.chdir(ROOT)  # snippets may reference scripts/ and tests/fixtures/
    exec(compile(code, "<doc>", "exec"), {"__name__": "__doc__"})
