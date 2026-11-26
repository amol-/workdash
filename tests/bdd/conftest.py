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
    analysis_tui as _analysis_tui,
)
from .steps import (
    api as _api,
)
from .steps import (
    autoclose as _autoclose,
)
from .steps import autodispatch as _autodispatch
from .steps import (
    branchdiff as _branchdiff,
)
from .steps import (
    branchinfo as _branchinfo,
)
from .steps import (
    cli_orchestration as _cli_orchestration,
)
from .steps import (
    common as _common,
)
from .steps import (
    dispatch as _dispatch,
)
from .steps import (
    ready as _ready,
)
from .steps import review as _review
from .steps import (
    setup as _setup,
)
from .steps import (
    show_config as _show_config,
)
from .steps import (
    startup as _startup,
)
from .steps import (
    terminal as _terminal,
)
from .steps import (
    todo as _todo,
)
from .steps import (
    triage as _triage,
)
from .steps import (
    view as _view,
)
from .steps import view_menu as _view_menu
from .steps import viewer_links as _viewer_links
from .steps import (
    worktrees as _worktrees,
)

for _step_module in (
    _common,
    _api,
    _autoclose,
    _autodispatch,
    _branchdiff,
    _cli_orchestration,
    _triage,
    _worktrees,
    _setup,
    _startup,
    _analysis_tui,
    _terminal,
    _show_config,
    _todo,
    _branchinfo,
    _ready,
    _review,
    _dispatch,
    _view,
    _view_menu,
    _viewer_links,
):
    for _name in dir(_step_module):
        if _name.startswith("pytestbdd_stepdef_") or _name in {
            "scenario_state",
            "analysis_dashboard",
            "review_state",
            "work_items",
            "valid_config",
            "autodispatch_state",
            "dashboard_dispatch",
            "setup_cli",
            "worktree_remote",
            "include_backend",
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
