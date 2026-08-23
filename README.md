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
- Authenticated [`gh`](https://cli.github.com/) for GitHub access.
  `workdash --configure` installs a local copy when `gh` is not available.
- `xdg-open` or `open` for opening links and rendered analyses in your browser.
- `zellij` for the interactive dashboard, terminal panes, and agent sessions.
  `workdash --configure` installs a local copy when `zellij` is not available.
- Global `gh` and `zellij` binaries are preferred; configured local copies are
  used as fallbacks.
- Optional, depending on which actions you use from the TUI:
  - `claude` — to analyze or launch a Claude coding session.
  - `codex` — to analyze or launch a ChatGPT Codex session.
  - `code` — to launch a VSCode + Copilot coding session.
  - `pi` — to launch a pi coding agent session.

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
- The todo repository where captured todos are created (defaults to
  `<username>/todos`, see *Todos* below).
- A working directory where per-item git worktrees will live.
- The analyze and launch commands for Claude and Codex (auto-detected when the
  tools are on your `PATH`).

You can edit `~/.config/workdash/config.json` by hand at any time. The `open`
setting controls the command used to open URLs and rendered analyses. The wizard
prompts for `open` when it is missing or malformed. Re-running `--configure`
only prompts for fields that are still empty, except for Zellij:
the wizard always checks for a global `zellij` first and downloads a fresh
local binary when no global binary exists. It does the same for `gh`, checking
for a global GitHub CLI first and downloading a fresh local copy when none is
available.

## Usage

Launch the TUI:

```bash
workdash
```

Useful commands and flags:

```bash
workdash --server            # start the TUI plus localhost JSON control API
workdash list                # list server-backed dashboard items as plain text
workdash list --refresh      # ask the server to refresh items before listing
workdash list --json         # list items as machine-readable JSON
workdash info [--all] [--json]  # report live Workdash-owned Zellij panes
workdash analyze ITEM [--agent NAME] [--json]  # analyze a current server item
workdash code ITEM [--agent NAME] [--json]  # launch a terminal-backed coding agent
workdash terminal ITEM [--json]            # open a plain terminal in an item's worktree
workdash todo TEXT [--target owner/repo] [--json]  # capture a todo
workdash read PANE_ID [--full] [--json]  # read text from a live pane
workdash write PANE_ID TEXT [--raw] [--json]  # send text to a live pane
workdash show-config [--json] # show configured agents and fixed server address
workdash --debug             # verbose logging
workdash --configure         # run the interactive setup wizard
workdash --direct            # start without the automatic Zellij wrapper
workdash --version           # print version and exit
```

## What gets shown

`workdash` gathers the following for your configured GitHub username:

- Open pull requests you authored (shown as `PR`).
- Open pull requests where you are a requested reviewer, plus open pull
  requests you have already reviewed (shown as `REVIEW`).
- Open issues assigned to you (shown as `ISSUE`).
- Open issues and pull requests across the tracked repositories. A pull request
  that is neither yours nor yours to review is shown as `CHECK`; once you review
  it, the next refresh lists it as `REVIEW`.

Items are merged, deduplicated, and sorted by last update. A pull request that
closes an issue replaces that issue, so the same work never takes two rows. One
item is marked with `*` as the suggested next thing to pick up.

### Repository selectors

The `repositories` list in `config.json` accepts:

- `owner/repo` — a specific repository.
- `owner/*` — every repository accessible to you under that owner.

## TUI keybindings

- `/` — search/filter the listed items by Type, Repo, or Title. Press `/` again
  to clear.
- `o` — open the selected item in your browser.
- `r` — refresh the list from GitHub.
- `a` — analyze the selected item. Opens a dialog to open the cached analysis
  (if any), or run a fresh analysis with Claude or Codex. Fresh analyses are
  cached and rendered as HTML in your browser.
- `c` — launch a coding session on the selected item. Opens a dialog to pick
  Claude, Codex, VSCode Copilot, or pi. `workdash` prepares a dedicated git
  worktree for the item and starts the chosen tool in it, preloaded with
  context about the issue or PR.
- `t` — open a terminal in the selected item's worktree (no agent launched).
- `w` — capture a todo. Opens a dialog asking for the todo text and an
  optional target repository.
- `q` — quit.

## Todos

Ideas and chores that show up while triaging can be captured as todos with `w`
in the TUI, `workdash todo TEXT`, or the JSON API. A todo is an ordinary GitHub
issue created in the single `todo_repository` from `config.json`, assigned to
you and labeled `workdash-todo`, so it shows up on the dashboard like any other
assigned issue. Create the repository on GitHub yourself; the label is created
on first use.

A todo may name a target repository (`--target owner/repo`). The issue still
lives in the todo repository, but the item is listed under its target and its
coding, terminal, and analysis actions run in a worktree of the target checked
out on a `wt-<number>` branch.

## Worktrees

Coding and analysis actions operate on a per-item git worktree rooted under the
configured `workdir`. Each tracked repository gets a local clone, and each work
item gets its own worktree alongside it, so you can hop between items without
disturbing other in-progress work.

A pull request you authored that closes an issue in the same repository is the
implementation of that issue, so it shares the issue's worktree directory (named
after the issue number) while staying checked out on the pull request's own
branch. That shared checkout is only reused while it holds the pull request's
branch; a checkout sitting on another branch belongs to other work, so it is left
alone and the pull request gets its own pull-request-numbered worktree instead.
Worktree directories created before this behavior existed are still named
after the pull request number; they are not migrated and keep working, so the
pull request stays in the checkout it already has and only a pull request with no
checkout yet gets an issue-numbered one. The link between a pull request and the
issue it closes is resolved while the dashboard refreshes, so a pull request
added by URL only shares the issue's worktree from the next refresh onwards.

## Local control server

`workdash --server` starts the normal TUI and a localhost JSON control API in the
same process. V0 listens on `127.0.0.1:8765`, accepts JSON only, and stops when
the TUI exits. Client commands such as `list`, `info`, `analyze`, `code`, `todo`,
`read`, and `write` connect to this server instead of loading GitHub or Zellij
state themselves.

## List command

`workdash list` requires a running `workdash --server` session and emits one row
per server-known item, sorted by last update (most recent first):

```
TYPE   repo#TYPE-N   YYYY-MM-DD   title
```

`TYPE` is `ISSUE`, `PR` (a PR you authored), `REVIEW` (for review-requested or
already reviewed PRs), or `CHECK` (any other PR). The row ID is
copy/paste-friendly: `repo#ISSUE-N`, `repo#PR-N`, `repo#REVIEW-N`,
`repo#CHECK-N`, or `<target>#ISSUE-WT<n>` for a targeted todo. The suggested
item's title is prefixed with `* `. If nothing matches, the output is
`No work items found.`.

Use `workdash list --refresh` to ask the server to refresh GitHub data before
listing. Use `workdash list --json` to emit the same list as JSON records with
item ID, type, kind, repository, number, title, URL, timestamps, and suggested
status.

## Info command

`workdash info [--json]` requires a running `workdash --server` session and
reports live Workdash-owned Zellij panes for terminal-backed work actions,
including pane title, cwd, command, tab, state, and mapped Workdash item. It maps
each live pane's current working directory to the matching Workdash item ID when
the pane is inside a known worktree, or reports `unknown` when no mapping is
known. Add `--all` to include other live non-plugin panes from the server-backed
Workdash session as `kind=unknown` with unknown item mapping.

## Analyze command

`workdash analyze ITEM [--agent NAME] [--json]` requires a running
`workdash --server` session and analyzes a current server-known Workdash item.
`ITEM` can be a row ID from `workdash list` (such as `owner/repo#ISSUE-123`) or a
GitHub issue/PR URL that is already in the current dashboard data. It reuses a
fresh cached analysis when available, otherwise the server prepares the item's
worktree and runs the selected configured analysis agent.

The server analyze response includes `content_type` (`text/markdown`), `file_name`,
and base64 `file_content` so clients do not need filesystem access to the server
cache. The CLI writes that content to a secure temporary local file and reports it
as `analysis_path`. `--json` emits the same client-side result without the raw
base64 content.

## Code command

`workdash code ITEM [--agent NAME] [--json]` requires a running
`workdash --server` session and launches a configured terminal-backed coding
agent (`claude`, `codex`, or `pi`) for a current server-known Workdash item.
`ITEM` accepts the same row IDs and GitHub URLs as `workdash analyze`.

Human output reports the item, agent, selected session, cwd, pane title, and pane
id when available. `--json` emits the same result as machine-readable JSON.

## Read and write commands

`workdash read PANE_ID [--full] [--json]` requires a running `workdash --server`
session and reads pane text through the server-backed pane content API. Human
output prints the pane text directly. Add `--full` to request full scrollback.

`workdash write PANE_ID TEXT [--raw] [--json]` requires a running
`workdash --server` session and sends pane input through the server-backed pane
send API. By default Workdash appends Enter; `--raw` or `--no-enter` sends the
text exactly. Human output confirms that the pane accepted the input.

## Show-config command

`workdash show-config [--json]` reports configured analysis agents, configured
terminal-backed coding agents, and the fixed V0 server address. It does not
require a running server. The same information is also available from the JSON
API for HTTP clients.

## Analysis cache

Analyses produced with `a` or `workdash analyze` are cached under
`~/.config/workdash/cache/` so that re-opening an item is instant. The cache is
keyed by the item's GitHub
`updated_at` timestamp, so any change on GitHub automatically invalidates the
cached analysis and the next analyze action will re-run it.

## Product behavior

Canonical product behavior is described in [`features/`](features/).

The `.feature` files are the source of truth for what the software does. They are product specifications first and executable BDD scenarios second.

Implementation progress can be tracked separately.
BDD is the integration point between humans and agents collaborating on the project as Workdash relied hevily on agents during its initial development cycle.

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
