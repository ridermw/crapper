---
name: crapper
description: Use when measuring or lowering CRAP scores, when a function is complex and under-tested, when crapper prints a high score or exits 2 under --threshold, when coverage reads 0% unexpectedly, or when .metrics/crap.edn must be refreshed for uml-viewer, in Clojure, Java, Go, TypeScript, Rust, or Python projects.
---

# crapper

## Overview

`CRAP = CC² × (1 − coverage)³ + CC`, where CC is cyclomatic complexity.
1–5 is low risk, 5–30 is worth a look, and over 30 is complex and
under-tested. Lower it by testing the function (coverage), then splitting it
(complexity). Tests come first because they make the split safe.

## Run

Run from the examined project's root. Use `crapper` if it is on `PATH`;
otherwise run `<base>/../../../crapper`, where `<base>` is this skill's base
directory (Python 3.11+; the first run builds the tool's own `.venv`).

| Command | Use |
|---|---|
| `crapper` | whole project with fresh coverage: **the run whose snapshot you keep** |
| `crapper src/pkg/pricing.py`, `crapper pricing`, `crapper --changed` | quick look at a subset while working |
| `crapper --use-existing-coverage` | reuse coverage reports already on disk |
| `crapper --coverage-command "<cmd>"` | replace the default coverage command |
| `crapper --no-coverage` | complexity only; CRAP is N/A |
| `crapper --threshold 30` | gate: exits 2 when the worst score is higher |

## Snapshot rules

- **Every run replaces `.metrics/crap.edn` with only what it scored.** A
  path, fragment, or `--changed` run leaves a partial snapshot. Always finish
  with a plain `crapper`.
- Coverage comes from each language's test command (coverage.py with pytest,
  `clj -M:cov --lcov`, JaCoCo, `go test -coverprofile`, `npm run coverage`,
  Vitest or c8, `cargo llvm-cov`). If the tests fail or the tool is missing,
  that language scores **0% and the snapshot is still written**. 0% everywhere
  means fix the test run, then rerun. Never refactor on that number.
- A function the coverage report does not mention scores 0%. A renamed or
  moved function is a new entry.

## Lowering a score

1. Pin behavior: write tests for the function as it is, covering each rule
   and boundary. Run them.
2. Prove the tests bite: run mutator on the file (REQUIRED SUB-SKILL:
   mutator) and kill the survivors before refactoring.
3. Extract Method in small steps, one named function per rule. Keep the
   tests green after each step.
4. Run a plain `crapper`. Report the function's before and after scores and
   anything new over 30.

Never delete tests, assertions, or reachable code paths to move the number.

## `.metrics/` is not clutter

`.metrics/` holds the snapshots uml-viewer reads (`crap.edn`, `mutate/`,
`dry.edn`). Never delete it or gitignore it to tidy up.

- `git ls-files .metrics` lists files: commit the refreshed `crap.edn` with
  the change.
- It lists nothing: leave `.metrics/` untracked and tell the user it is
  there (uml-viewer recommends committing it).

## Red flags

| Thought | Do instead |
|---|---|
| "One last `crapper src/x.py` to confirm" | Finish with plain `crapper`. |
| "Coverage is 0% everywhere" | The test run failed. Fix it first. |
| "Delete `.metrics/` so the tree is tidy" | Keep it and mention it. |

## More

- [crapper README](../../../README.md)
- [crap4clj](https://github.com/unclebob/crap4clj), the Clojure original
- [Pardon My French, But This Code Is C.R.A.P. (2)](https://www.artima.com/weblogs/viewpost.jsp?thread=210575), the formula's origin
- [The Cycles of TDD](https://blog.cleancoder.com/uncle-bob/2014/12/17/TheCyclesOfTDD.html)
