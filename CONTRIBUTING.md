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

## Releases

Releases are cut by pushing a version tag in `N.N.N` format. The tag must match
the `version` field in `pyproject.toml`.

The release workflow validates the tag, runs lint and tests, builds the wheel
and source distribution, attaches both artifacts to a generated GitHub Release,
and publishes the distribution to PyPI through PyPI Trusted Publishing.

PyPI must be configured with a trusted publisher for this repository and the
`.github/workflows/release.yml` workflow before the first publish can succeed.

## Reporting bugs

Open an issue at <https://github.com/amol-/workdash/issues> with:

- what you ran,
- what you expected,
- what happened instead.

## License

By contributing, you agree that your contributions will be licensed under
the project's GPLv3 license.
