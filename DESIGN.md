# Design direction

A change in an agent pane's content means the agent is doing work. Treat any
content change as activity and recovery from an idle or blocked state, regardless
of whether it followed human input or the agent resumed on its own. Do not try
to distinguish the cause of the change.
