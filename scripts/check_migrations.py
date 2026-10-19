"""Validate a migrations folder's revision graph without importing any app code.

Each file sets module-level ``revision`` and ``down_revision`` (Alembic style). Importing them would
run the app's imports and need its dependencies and a database; reading them with ``ast`` needs
nothing. Catches: duplicate ids (two branches merged with the same id), a parent that does not
exist, cycles, and more than one head (a fork nobody merged). Each of these crashes a deploy at boot.

Usage: python scripts/check_migrations.py path/to/migrations
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _read(path: Path) -> tuple[str | None, list[str]]:
    values: dict[str, object] = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
            if name in ("revision", "down_revision"):
                values[name] = ast.literal_eval(node.value)
    rev = values.get("revision")
    down = values.get("down_revision")
    parents = [] if down is None else [down] if isinstance(down, str) else list(down)  # type: ignore[arg-type]
    return (rev if isinstance(rev, str) else None), parents


def check(folder: str) -> list[str]:
    problems: list[str] = []
    graph: dict[str, list[str]] = {}
    for path in sorted(Path(folder).glob("*.py")):
        rev, parents = _read(path)
        if rev is None:
            problems.append(f"{path.name}: no revision id")
        elif rev in graph:
            problems.append(f"duplicate revision id {rev!r} ({path.name})")
        else:
            graph[rev] = parents
    for rev, parents in graph.items():
        for parent in parents:
            if parent not in graph:
                problems.append(f"revision {rev!r} has missing parent {parent!r}")
    for start in graph:  # follow parents; seeing a node twice on one walk is a cycle
        seen, stack = set(), [start]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(p for p in graph.get(node, []) if p in graph)
            if start in graph.get(node, []):
                problems.append(f"cycle through revision {start!r}")
                break
    heads = sorted(set(graph) - {p for parents in graph.values() for p in parents})
    if len(heads) > 1:
        problems.append(f"multiple heads: {', '.join(heads)}")
    return sorted(set(problems))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__)
        return 2
    problems = check(args[0])
    for problem in problems:
        print(f"check_migrations: {problem}", file=sys.stderr)
    if not problems:
        print(f"check_migrations: {args[0]} ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
