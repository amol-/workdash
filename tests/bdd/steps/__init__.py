"""Step definitions for BDD scenarios.

Each module in this package defines steps for one product domain, mirroring
the ``features/<domain>/`` layout. Importing these modules here registers
their ``@given``/``@when``/``@then`` decorators with pytest-bdd, via the
re-export in ``tests/bdd/conftest.py``.

Shared steps that appear in more than one feature file live in ``common``.
"""

from . import (
    analysis,  # noqa: F401
    branchdiff,  # noqa: F401
    browse,  # noqa: F401
    coding,  # noqa: F401
    common,  # noqa: F401 - shared step defs and fixtures
    setup,  # noqa: F401
    startup,  # noqa: F401
    terminal,  # noqa: F401
    triage,  # noqa: F401
    worktrees,  # noqa: F401
)
