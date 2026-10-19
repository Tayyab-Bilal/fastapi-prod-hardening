"""Fail a CI job when its JUnit report is missing, empty, or has nothing but skips.

"No failures" is not the same as "tests ran". A suite that crashed before collecting, or whose
database was unreachable so every test skipped, produces a report with no failures. This script
turns those cases into a red build.

Usage: python scripts/check_junit.py report.xml [--max-skips N]
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def check(path: str, max_skips: int | None = None) -> list[str]:
    """Return a list of problems (empty means the report is acceptable)."""
    report = Path(path)
    if not report.is_file():
        return [f"report not found: {path}"]
    try:
        root = ET.parse(report).getroot()
    except ET.ParseError as exc:
        return [f"report is not valid XML: {exc}"]
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = sum(int(s.get("tests", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    failed = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
    problems = []
    if total == 0:
        problems.append("report contains zero tests")
    elif skipped == total:
        problems.append(f"all {total} tests were skipped")
    if failed:
        problems.append(f"{failed} test(s) failed or errored")
    if max_skips is not None and skipped > max_skips:
        problems.append(f"{skipped} skipped tests, allowed {max_skips}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report")
    parser.add_argument("--max-skips", type=int, default=None)
    args = parser.parse_args(argv)
    problems = check(args.report, args.max_skips)
    for problem in problems:
        print(f"check_junit: {problem}", file=sys.stderr)
    if not problems:
        print(f"check_junit: {args.report} ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
