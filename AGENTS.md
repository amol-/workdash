# Notes for agents working on this repository

Before starting any work, read these files in order:

1. **README.md** – Project overview, installation, and usage.
2. **CONTRIBUTING.md** – Core rules, especially BDD-first workflow and the distinction between user-facing BDD scenarios and implementation-detail tests.

## Quick reminders

- BDD scenarios describe user-visible, API-visible, or externally observable behavior only.
- Implementation details, corner cases, and internal mechanisms go in normal unit/integration tests.
- Keep changes minimal and focused.
- Match existing style; see `pyproject.toml` for lint/format config and run `ruff check` and `ruff format`.
