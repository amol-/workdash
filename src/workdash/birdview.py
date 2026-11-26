"""A shareable snapshot of the running dashboard."""

import re

from .models import WorkItemType

DEFAULT_TOP = 5
FORMATS = ("telegram", "markdown", "json")


def birdview(session, *, top: int, format: str) -> dict:
    from .control import WorkdashControlError, format_work_item_id

    info = session.info(include_shared_items=True)
    visible = session.list_items()["items"]
    visible_ids = {item["id"] for item in visible}
    with session._lock:
        issues = {
            (item.repo.lower(), item.number): item
            for item in session.work_items
            if item.item_type == WorkItemType.ISSUE
        }
        prs = [
            {
                "id": format_work_item_id(item),
                "title": item.title,
                "url": item.url,
                "review_decision": item.review_decision,
                "review_requested": item.review_requested,
                "has_unresolved_comments": item.has_unresolved_comments,
            }
            for item in session.work_items
            if item.item_type == WorkItemType.PR
            and item.state.upper() == "OPEN"
            and format_work_item_id(item) in visible_ids
        ]
    sessions = [
        entry
        for entry in info["autodispatch_sessions"]
        if entry["status"] in ("running", "blocked")
    ]
    for entry in sessions:
        target = issues.get(session._scope_identity(entry.get("scope") or ""))
        entry["sub_issue_total"] = target.sub_issue_total if target else None
    pane_ids = {str(entry["pane_id"]) for entry in sessions if entry.get("pane_id") is not None}
    agents = [
        pane
        for pane in info["panes"]
        if pane["kind"] == "agent" and str(pane["pane_id"]) not in pane_ids
    ]
    sections = {
        "autodispatch": sessions,
        "running_agents": agents,
        "open_pull_requests": prs,
        "recent_items": visible,
    }
    snapshot = {
        name: {"items": items[:top], "omitted": max(0, len(items) - top)}
        for name, items in sections.items()
    }
    if format == "json":
        return snapshot

    telegram = format == "telegram"

    def escape(value):
        value = re.sub(r"[\x00-\x1f\x7f\x1b]+", " ", str(value))
        return (
            re.sub(r"([_\*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", value)
            if telegram
            else re.sub(r"([\\\[\]*_`])", r"\\\1", value)
        )

    def link(label, url):
        if not url:
            return escape(label)
        return f"[{escape(label)}]({str(url).replace(')', '%29').replace(' ', '%20')})"

    def reference_link(label):
        try:
            repo, number, item_type, _ = session._item_target(label)
        except WorkdashControlError:
            return escape(label)
        kind = "issues" if item_type == WorkItemType.ISSUE else "pull"
        return link(label, f"https://github.com/{repo}/{kind}/{number}")

    lines = []
    for name, heading in (
        ("autodispatch", "Autodispatch"),
        ("running_agents", "Running agents"),
        ("open_pull_requests", "Open pull requests"),
        ("recent_items", "Recent items"),
    ):
        entries = []
        for item in snapshot[name]["items"]:
            if name == "autodispatch":
                scope = item.get("scope") or "unknown"
                target = reference_link(scope)
                current = item.get("current_item")
                details = f"{target} · {escape(item['status'])} · {escape(item.get('stage') or '')} · {escape(item.get('iteration', 0))}"
                if item.get("sub_issue_total"):
                    details += " · " + escape(f"{item['sub_issue_total']} sub-issues")
                if current:
                    details += " · " + reference_link(current)
                if item.get("agent"):
                    details += " · " + escape(item["agent"])
                if item.get("pane_id") is not None:
                    details += " · pane " + escape(item["pane_id"])
            elif name == "running_agents":
                references = item.get("items") or [
                    {"id": item["item"], "url": item.get("item_url")}
                ]
                details = " · ".join(link(ref["id"], ref["url"]) for ref in references)
                details += " · pane " + escape(item["pane_id"])
                if item.get("command"):
                    details += " · " + escape(item["command"])
            else:
                details = link(item["id"], item.get("url")) + " · " + escape(item["title"])
                if name == "open_pull_requests":
                    if item["review_decision"]:
                        details += " · " + escape(item["review_decision"])
                    if item["has_unresolved_comments"]:
                        details += " · " + escape("unresolved comments")
                    elif not item["review_decision"]:
                        details += " · " + escape(
                            "review requested" if item["review_requested"] else "review unknown"
                        )
            entries.append((r"\- " if telegram else "- ") + details)
        omitted = snapshot[name]["omitted"]
        heading_line = f"*{heading}*" if telegram else f"## {heading}"
        # Reserve room for later headings and omission counts.
        lines.append(heading_line)
        for index, entry in enumerate(entries):
            if telegram and len("\n".join(lines + [entry])) > 3800:
                omitted += len(entries) - index
                break
            lines.append(entry)
        if omitted:
            lines.append(escape(f"… and {omitted} more"))
        elif not entries:
            lines.append(escape("None"))
    return {"text": "\n".join(lines)}
