"""Auto-generate one pytest test per scenario under ``features/``.

Every ``Scenario`` in every ``.feature`` file becomes a pytest test via
``pytest_bdd.scenarios``. Tests whose steps are not yet implemented fail
with ``StepDefinitionNotFoundError`` — the intended signal to drive
implementation from the spec.
"""

from __future__ import annotations

from pathlib import Path

from pytest_bdd import scenarios

scenarios(str(Path(__file__).resolve().parents[2] / "features"))
