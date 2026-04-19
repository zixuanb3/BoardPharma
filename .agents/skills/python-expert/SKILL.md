---
name: python-expert
description: Senior Python development guidance for writing clean, efficient, well-documented, and type-safe Python code. Use when Codex needs to write or refactor Python, review Python for correctness and maintainability, debug Python exceptions, add type hints or dataclasses, improve performance, or apply PEP 8 and docstring conventions.
---

# Python Expert

## Overview

Apply senior-level Python engineering judgment when writing, reviewing, debugging, or refining Python code. Optimize for correctness first, then type safety, performance, readability, and clear verification.

## Workflow

1. Read the target files, traceback, tests, or user prompt closely before changing code.
2. Fix correctness risks first: mutable defaults, bad exception handling, resource leaks, edge cases, and unsafe assumptions about input data.
3. Add or improve type information on public APIs and non-trivial helpers. Prefer `dataclass` for data containers and precise container types over vague annotations.
4. Prefer standard-library solutions and idiomatic Python: comprehensions when clearer, generators for streams, `with` statements for resources, `pathlib` for paths, and f-strings for formatting.
5. Keep style clean and conventional. Use descriptive names, brief comments only for non-obvious logic, and docstrings on public functions, classes, and modules.
6. Run project-relevant checks when available and report any unverified assumptions or missing test coverage.

## Review Priorities

Use this order when reviewing or refactoring:

1. Correctness
2. Type safety
3. Performance and resource handling
4. Style, readability, and documentation
5. Test coverage and maintainability

## Output Expectations

- Show concrete fixes, not only abstract advice.
- Include type hints in Python examples unless the surrounding codebase clearly avoids them.
- Use specific exceptions instead of bare `except:`.
- Preserve behavior unless the task explicitly asks for a semantic change.
- When reviewing code, lead with the most important findings and include file and line references when available.

## References

Read [references/python-review-guide.md](references/python-review-guide.md) when you need the detailed checklist, common pitfalls, or example patterns for reviews and refactors.
