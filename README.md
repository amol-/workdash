# workdash

Text-based GitHub work triage dashboard ( <https://alessandro.molina.fyi/workdash/> )

`workdash` pulls together the issues and pull requests that matter to you across
your GitHub repositories, suggests what to pick up next, and makes it easy to
jump into a work item — either for a quick review, a deeper AI-assisted
analysis, or a full coding session in a dedicated worktree.

![workdash screenshot](site/workdash.png)

## Platform

Linux and macOS. `workdash` relies on `xdg-open` or `open` and expects `zellij`
for the interactive dashboard and terminal-backed work actions. It has not
been tested on Windows.

## Requirements

- Python `>=3.12`.
- [`gh`](https://cli.github.com/) authenticated (`gh auth status` must
  succeed). The `workdash --configure` wizard detects a `gh` binary on `PATH`;
  when none is available, it installs a local copy under
  `~/.config/workdash/` from <https://github.com/cli/cli/releases/latest>.
  The authenticated account should have access to the repositories you want to
  track; if GitHub requires additional repository authorization for a repository
  while loading a specific work source, `workdash` warns, skips that inaccessible
  repository or item, and still loads the others.
- `xdg-open` or `open` for opening links and rendered analyses in your browser.
- `zellij` for the interactive dashboard, terminal panes, and agent sessions.
  The `workdash --configure` wizard detects a `zellij` binary on `PATH`; when
  none is available, it can install a local copy under `~/.config/workdash/`.
  When `workdash` starts outside Zellij, it prefers your global `zellij` when
  one is available, otherwise uses the local configured copy, starts a fresh
  `workdash-<random>` session, and runs the dashboard inside it.
- On every startup, `workdash` prefers your global GitHub CLI when one is
  available, otherwise uses the local configured copy.
- Optional, depending on which actions you use from the TUI:
  - `claude` — to analyze or launch a Claude coding session.
  - `codex` — to analyze or launch a ChatGPT Codex session.
  - `code` — to launch a VSCode + Copilot coding session.

## Installation

With [`uv`](https://docs.astral.sh/uv/) — one isolated install, `workdash` on
your `PATH`:

```bash
uv tool install workdash
```

For development install:

```bash
pip install -e '.[test]'
```

## First-time setup

On the first run you need a configuration file at
`~/.config/workdash/config.json`. Generate it interactively with:

```bash
workdash --configure
```

The wizard fills in:

- Your GitHub username.
- Zellij availability. If `zellij` is not on `PATH`, the wizard downloads the
  latest release binary into the workdash configuration directory.
- GitHub CLI availability. If `gh` is not on `PATH`, the wizard downloads the
  latest release binary into the workdash configuration directory. Authentication
  is still handled by `gh`; run `gh auth login` if startup reports that `gh` is
  not authenticated.
- The list of repositories to track (see *Repository selectors* below).
- A working directory where per-item git worktrees will live.
- The analyze and launch commands for Claude and Codex (auto-detected when the
  tools are on your `PATH`).

You can edit `~/.config/workdash/config.json` by hand at any time. Re-running
`--configure` only prompts for fields that are still empty, except for Zellij:
the wizard always checks for a global `zellij` first and downloads a fresh
local binary when no global binary exists. It does the same for `gh`, checking
for a global GitHub CLI first and downloading a fresh local copy when none is
available.

## Usage

Launch the TUI:

```bash
workdash
```

Useful flags:

```bash
workdash --print       # list items as plain text, no TUI
workdash --refresh     # force a refresh from GitHub
workdash --debug       # verbose logging
workdash --configure   # run the interactive setup wizard
workdash --direct      # start without the automatic Zellij wrapper
workdash --version     # print version and exit
```

## What gets shown

`workdash` gathers the following for your configured GitHub username:

- Open pull requests you authored.
- Open pull requests where you are a requested reviewer, plus open pull
  requests you have already reviewed (shown as `REVIEW`).
- Open issues assigned to you.
- Open issues and pull requests across the tracked repositories.

Items are merged, deduplicated, and sorted by last update. One item is marked
with `*` as the suggested next thing to pick up.

### Repository selectors

The `repositories` list in `config.json` accepts:

- `owner/repo` — a specific repository.
- `owner/*` — every repository accessible to you under that owner.

## TUI keybindings

- `o` — open the selected item in your browser.
- `r` — refresh the list from GitHub.
- `a` — analyze the selected item. Opens a dialog to open the cached analysis
  (if any), or run a fresh analysis with Claude or Codex. Fresh analyses are
  cached and rendered as HTML in your browser.
- `c` — launch a coding session on the selected item. Opens a dialog to pick
  Claude, Codex, or VSCode Copilot. `workdash` prepares a dedicated git
  worktree for the item and starts the chosen tool in it, preloaded with
  context about the issue or PR.
- `t` — open a terminal in the selected item's worktree (no agent launched).
- `q` — quit.

## Worktrees

Coding and analysis actions operate on a per-item git worktree rooted under the
configured `workdir`. Each tracked repository gets a local clone, and each work
item gets its own worktree alongside it, so you can hop between items without
disturbing other in-progress work.

## Print mode

`--print` emits one row per item, sorted by last update (most recent first):

```
TYPE   repo#number   YYYY-MM-DD   title
```

`TYPE` is `ISSUE`, `PR`, or `REVIEW` (for review-requested PRs). The suggested
item's title is prefixed with `* `. If nothing matches, the output is
`No work items found.`.

## Analysis cache

Analyses produced with `a` are cached under `~/.config/workdash/cache/` so that
re-opening an item is instant. The cache is keyed by the item's GitHub
`updated_at` timestamp, so any change on GitHub automatically invalidates the
cached analysis and the next `a` will re-run it.

## Product behavior

Canonical product behavior is described in [`features/`](features/).

The `.feature` files are the source of truth for what the software does. They are product specifications first and executable BDD scenarios second.

Implementation progress can be tracked separately, for example in `PROJECT_PLAN.md`, by referencing stable feature and scenario IDs.

### BDD-first development (mandatory)

Every new feature **must** start with a BDD definition under [`features/`](features/), and every change to existing behavior **must** update the relevant `.feature` file **before** any implementation or test code is modified. The `.feature` files are the contract; code follows them, not the other way around.

This applies to agents and humans alike. A change that lands code without a matching `features/` update is incomplete.

## Tests

From the repository root:

```bash
PYTHONPATH=src pytest
```

### BDD suite

`tests/bdd/` drives development from the `.feature` files in `features/`.
`pytest_bdd.scenarios(...)` auto-generates one pytest test per `Scenario`,
so adding a scenario to `features/` is enough to pull it into the suite.
Step definitions live under `tests/bdd/steps/`; scenarios whose steps are
not yet implemented fail with `StepDefinitionNotFoundError`, which is the
intended TDD signal for BDD-first development.

## License

`workdash` is distributed under the terms of the GNU General Public License
version 3. See [LICENSE](LICENSE) for the full text.
