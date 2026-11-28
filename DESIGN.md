# Design direction

A change in an agent pane's content means the agent is doing work. Treat any
content change as activity and recovery from an idle or blocked state, regardless
of whether it followed human input or the agent resumed on its own. Do not try
to distinguish the cause of the change.

A Zellij session has one environment, so it has one Workdash address: every
pane inherits the environment the session started with. Pick the address before
the session starts, and have every command act on the session's dashboard rather
than an address of its own. Never pass an address to the panes Workdash opens.
