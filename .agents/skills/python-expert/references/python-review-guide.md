# Python Review Guide

Use this guide when a Python task needs more than a quick patch.

## Priority Order

1. Correctness
2. Type safety
3. Performance and resource handling
4. Style and documentation
5. Tests and maintainability

## Correctness Checks

- Replace mutable default arguments with `None` plus in-function initialization.
- Replace bare `except:` with specific exceptions and actionable error handling.
- Check boundary cases, empty inputs, missing keys, `None` handling, and implicit type coercions.
- Use context managers for files, locks, and other resources.
- Prefer explicit validation when bad inputs would otherwise fail deep inside the call stack.

## Type Safety Checks

- Add annotations to function parameters and return values.
- Prefer concrete container element types over `list` or `dict` without parameters.
- Use `dataclass` for structured records or configuration objects.
- Keep optionality explicit with `X | None` or `Optional[X]`.

## Performance and Clarity

- Prefer comprehensions for short transformations and filters.
- Prefer generators or iterators for large streams.
- Use built-in modules such as `collections`, `itertools`, `pathlib`, and `functools` when they simplify the code.
- Optimize only after preserving readability and understanding the hot path.

## Style and Documentation

- Follow PEP 8 naming and spacing conventions.
- Use docstrings on public modules, classes, and functions.
- Keep comments for non-obvious logic, invariants, or external constraints.
- Prefer descriptive names over abbreviations unless the domain convention is clear.

## Review Output

When reviewing code, report issues in severity order and tie each point to a concrete risk: wrong results, crashes, leaked resources, type confusion, missing tests, or maintainability problems.
