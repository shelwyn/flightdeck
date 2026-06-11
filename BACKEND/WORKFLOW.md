---
tracker:
  required_labels: []
  active_states: [Todo, In Progress]
  terminal_states: [Completed, Cancelled]
  # Flight Deck-owned writes (we, not the agent, update task state):
  comment_on_start: false
  comment_on_finish: true
polling:
  interval_ms: 15000
workspace:
  # Project workspaces live under WORKSPACES/<project_slug>/<task>/ (relative to FlightDeck/).
  root: ../WORKSPACES
hooks:
  timeout_ms: 60000
  # after_create: |
  #   git clone git@github.com:you/repo.git .
agent:
  max_concurrent_agents: 3
  max_turns: 10
  max_retry_backoff_ms: 300000
codex:
  # The agent app-server command. We bind Symphony's "codex" to Pi's RPC mode.
  command: pi --mode rpc
  turn_timeout_ms: 3600000
  read_timeout_ms: 15000
  stall_timeout_ms: 300000
server:
  # HTTP dashboard (observability only). Remove to disable; CLI --port overrides.
  host: 127.0.0.1
  port: 8787
---
You are an autonomous engineer working on one task of a larger project. The
current working directory is the SHARED project workspace: earlier tasks in this
project have already produced files here, and later tasks will build on yours.
Read what already exists before you start.

## Issue {{ issue.identifier }}: {{ issue.title }}

State: {{ issue.state }}
{% if issue.subdir %}Target subfolder: {{ issue.subdir }}/{% endif %}
{% if issue.priority %}Priority: {{ issue.priority }}{% endif %}
{% if issue.url %}Link: {{ issue.url }}{% endif %}
{% if issue.labels %}Labels: {% for label in issue.labels %}{{ label }} {% endfor %}{% endif %}

### Description

{{ issue.description }}

## Instructions

{% if attempt %}This is a retry/continuation (attempt {{ attempt }}). Review what already
exists in the workspace before redoing work.{% else %}This is the first attempt.{% endif %}

- Organize your work into meaningful subfolders within this shared workspace
  {% if issue.subdir %}(use `{{ issue.subdir }}/` for this task){% else %}(e.g. `backend/`, `frontend/`, `tests/`){% endif %}.
  Do not delete or rewrite unrelated files created by other tasks.
- Implement the change end to end: read context, edit code, and verify your work.
- Keep changes surgical and scoped to this issue.
- **Do not kill processes or run port-cleanup commands (`kill`, `fuser`, `lsof -t`) against port 8787** — that is the Flight Deck dashboard. Bind project APIs to another port (e.g. 8000, 8080).

## Finish protocol (required)

End your FINAL message with a verdict block, each item on its own line:

```
STATUS: PASS    # or FAIL
REASON: <one short sentence>
```

Use `STATUS: PASS` only when the work is genuinely complete, **deliverable files exist on disk in the project workspace**, and you have verified them.
Use `STATUS: FAIL` if the task cannot be completed; the orchestrator will retry.

Reporting PASS without creating or modifying files in the workspace will be rejected automatically.
In `REASON`, name the files you created (e.g. `analysis/report.md`).
