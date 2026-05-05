# Contributing to workdash

## Core rules

- **BDD first.** Every new feature must start with a scenario under
  [`features/`](features/). Every change to existing behavior must update
  the relevant `.feature` file **before** any implementation or test code
  is modified.
- **Tests must pass.** Run `PYTHONPATH=src pytest` from the repository root.
- **Match the existing style.** Run `ruff check src tests` and
  `ruff format src tests` before submitting.
- **Keep changes focused.** One logical change per pull request.
- **No unrelated cleanup.** Don't mix refactors with feature or bug fix PRs.

## Reporting bugs

Open an issue at <https://github.com/amol-/workdash/issues> with:

- what you ran,
- what you expected,
- what happened instead.

## License

By contributing, you agree that your contributions will be licensed under
the project's GPLv3 license.
