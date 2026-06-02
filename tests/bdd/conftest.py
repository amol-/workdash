"""Root conftest for BDD tests.

pytest-bdd 8.x injects every step's fixture into the module that called
the ``@given``/``@when``/``@then`` decorator. pytest fixture discovery
only picks up fixtures defined in conftests or in the test module, so we
eagerly import the step modules here and then re-export every
``pytestbdd_stepdef_*`` fixture they registered into this conftest's
namespace. That makes the step fixtures visible to the auto-generated
scenario tests without forcing production code to live in a single
monolithic conftest.
"""

from __future__ import annotations

from .steps import (
    analysis as _analysis,
)
from .steps import (
    api as _api,
)
from .steps import (
    browse as _browse,
)
from .steps import (
    cli_orchestration as _cli_orchestration,
)
from .steps import (
    coding as _coding,
)
from .steps import (
    common as _common,
)
from .steps import (
    setup as _setup,
)
from .steps import (
    startup as _startup,
)
from .steps import (
    show_config as _show_config,
)
from .steps import (
    terminal as _terminal,
)
from .steps import (
    triage as _triage,
)
from .steps import (
    worktrees as _worktrees,
)

for _step_module in (
    _common,
    _api,
    _cli_orchestration,
    _triage,
    _worktrees,
    _setup,
    _startup,
    _analysis,
    _coding,
    _browse,
    _terminal,
    _show_config,
):
    for _name in dir(_step_module):
        if _name.startswith("pytestbdd_stepdef_") or _name in {
            "scenario_state",
            "work_items",
            "valid_config",
            "config_path",
        }:
            globals()[_name] = getattr(_step_module, _name)


def pytest_bdd_apply_tag(tag, function):
    """Treat ``.feature`` tags as opaque labels rather than pytest markers.

    The default pytest-bdd behavior turns every tag into a pytest marker
    via ``getattr(pytest.mark, tag)``. Our tags follow the Gherkin
    convention of ``@kind:id`` (``@feature:F-...``, ``@id:F-...-S001``),
    which pytest's marker registry cannot represent, and scenario ids
    are unique per-scenario so registering them would be busywork.
    Returning ``True`` tells pytest-bdd the hook handled the tag; no
    marker is applied.
    """
    return True
