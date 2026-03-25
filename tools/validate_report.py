#!/usr/bin/env python3
"""Validate structured handoff reports from investigation sub-agents.

Checks YAML frontmatter, required sections, and categorized learnings.
Exit 0 = valid, exit 1 = issues found.

Usage:
    python tools/validate_report.py <file-or-dir>
    python tools/validate_report.py $WORKDIR/report-agent-a.md
    python tools/validate_report.py $WORKDIR/   # validates all report-*.md files
"""

import argparse
import re
import sys
from pathlib import Path

REQUIRED_FRONTMATTER_KEYS = {"agent", "target", "skill", "status", "findings_added"}
REQUIRED_SECTIONS = {"Key Discoveries", "Findings Added", "Learnings"}
VALID_LEARNING_CATEGORIES = {"Friction", "Surprise", "Methodology", "Process gap", "Source quality"}


def parse_frontmatter(text):
    """Extract YAML frontmatter between --- markers. Returns (dict, errors)."""
    errors = []
    match = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if not match:
        return {}, ["Missing YAML frontmatter (expected --- markers at start of file)"]

    frontmatter = {}
    for line in match.group(1).strip().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' not in line:
            errors.append(f"Malformed frontmatter line: {line}")
            continue
        key, _, value = line.partition(':')
        frontmatter[key.strip()] = value.strip().strip('"').strip("'")

    missing = REQUIRED_FRONTMATTER_KEYS - set(frontmatter.keys())
    if missing:
        errors.append(f"Missing frontmatter keys: {', '.join(sorted(missing))}")

    return frontmatter, errors


def parse_sections(text):
    """Extract H2 section headers. Returns (set of section names, errors)."""
    errors = []
    sections = set()
    for match in re.finditer(r'^## (.+)$', text, re.MULTILINE):
        sections.add(match.group(1).strip())

    missing = REQUIRED_SECTIONS - sections
    if missing:
        errors.append(f"Missing required sections: {', '.join(sorted(missing))}")

    return sections, errors


def parse_learnings(text):
    """Validate Learnings section entries have [Category] prefixes. Returns errors."""
    errors = []
    match = re.search(r'^## Learnings\s*\n(.*?)(?=^## |\Z)', text, re.MULTILINE | re.DOTALL)
    if not match:
        return errors  # section missing handled by parse_sections

    content = match.group(1).strip()
    if not content:
        errors.append("Learnings section is empty")
        return errors

    entries = re.findall(r'^- \[([^\]]+)\]', content, re.MULTILINE)
    if not entries:
        errors.append("Learnings entries must use [Category] prefix (e.g., '- [Friction] ...')")
        return errors

    for category in entries:
        if category not in VALID_LEARNING_CATEGORIES:
            errors.append(f"Unknown learning category: [{category}] (valid: {', '.join(sorted(VALID_LEARNING_CATEGORIES))})")

    return errors


def validate_report(filepath):
    """Validate a single report file. Returns list of error strings."""
    text = Path(filepath).read_text(encoding="utf-8")
    all_errors = []

    _, fm_errors = parse_frontmatter(text)
    all_errors.extend(fm_errors)

    _, sec_errors = parse_sections(text)
    all_errors.extend(sec_errors)

    learn_errors = parse_learnings(text)
    all_errors.extend(learn_errors)

    return all_errors


def main():
    parser = argparse.ArgumentParser(description="Validate structured handoff reports")
    parser.add_argument("path", help="Report file or directory containing report-*.md files")
    args = parser.parse_args()

    target = Path(args.path)
    if target.is_dir():
        files = sorted(target.glob("report-*.md"))
        if not files:
            print(f"No report-*.md files found in {target}")
            sys.exit(1)
    elif target.is_file():
        files = [target]
    else:
        print(f"Path not found: {target}")
        sys.exit(1)

    total_errors = 0
    for f in files:
        errors = validate_report(f)
        if errors:
            print(f"FAIL: {f.name}")
            for e in errors:
                print(f"  - {e}")
            total_errors += len(errors)
        else:
            print(f"OK: {f.name}")

    if total_errors:
        print(f"\n{total_errors} issue(s) found across {len(files)} file(s)")
        sys.exit(1)
    else:
        print(f"\n{len(files)} file(s) validated successfully")
        sys.exit(0)


if __name__ == "__main__":
    main()
