---
name: code-simplifier
description: Simplify and clean up code after functional changes are complete. Use when Codex should polish recently modified files, reduce complexity, remove duplication or dead code, improve readability, apply idiomatic patterns, and run formatting or tests while preserving behavior.
---

# Code Simplifier

## Overview

Run this skill after a feature, fix, or refactor is already working. Focus on making changed code smaller, clearer, and more maintainable without quietly changing behavior.

## Workflow

1. Determine scope first. Prefer the current diff or the files the user named.
2. Look for high-value simplifications: long functions, deep nesting, repetitive conditionals, duplicated logic, unused imports or variables, redundant branches, commented-out code, and noisy comments.
3. Apply small behavior-preserving edits. Prefer guard clauses, helper extraction, named constants, tighter loops, simpler boolean logic, and clearer data flow over large rewrites.
4. Use idiomatic patterns when they improve clarity: comprehensions, `enumerate`, `zip`, `with`, `pathlib`, and f-strings.
5. Re-run relevant formatters, linters, and tests after each batch of changes when the project provides them.
6. Summarize what was simplified, what was intentionally left alone, and any follow-up cleanup that still carries risk.

## Guardrails

- Preserve public behavior and interfaces unless the user asks for a broader redesign.
- Prefer smaller diffs over sweeping rewrites.
- Stop and call out ambiguous logic instead of "simplifying" code you do not yet understand.
- Remove comments only when the code becomes self-explanatory or the comment is clearly stale.
- Keep refactoring and bug fixing conceptually separate when possible.

## Common Transformations

- Break a long function into smaller single-purpose helpers.
- Replace nested `if` blocks with guard clauses or simpler predicates.
- Replace manual indexing loops with direct iteration or `enumerate`.
- Consolidate duplicate branches or shared post-processing.
- Replace ad hoc string formatting with f-strings.
- Remove dead code, unused imports, and temporary debugging leftovers.

## Verification

Use project-native checks when available. For Python projects, likely candidates are `ruff format`, `ruff check --fix`, and `pytest`, but follow the repository's actual toolchain instead of forcing generic commands.
