# Flight Deck

**Flight Deck** is a web control plane for running multi-agent coding sprints on top of [Pi](https://pi.dev/) — the minimal coding-agent harness from Earendil. You define projects and tasks, plan an iteration, and Flight Deck dispatches Pi agents into shared workspaces, tracks progress on a kanban board, runs adversarial QA when build work finishes, and surfaces artifacts, run history, and git activity in one dashboard.

Flight Deck does **not** replace Pi. Pi remains the LLM harness (models, tools, credentials, RPC protocol). Flight Deck is the **orchestrator**: concurrency, retries, iteration lifecycle, task dependencies, workspace layout, and the UI.

```
┌─────────────────────────────────────────────────────────────┐
│  Flight Deck (Flask dashboard + orchestrator)               │
│  projects · iterations · board · QA · artifacts · runs      │
└──────────────────────────┬──────────────────────────────────┘
                           │ spawn long-lived sessions
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Pi — pi --mode rpc  (JSONL over stdio)                     │
│  models · tools · extensions · credentials                  │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
              WORKSPACES/<project>/   (agent output on disk)
```

---

## Features

### Mission Control
- **Projects** with optional custom workspace paths and optional git integration
- **Iterations** (planning → running → testing → completed) scoped to a project
- **Kanban board**: Todo, In Progress, Completed, Verification (QA), Cancelled
- **Tasks and bugs** with descriptions, file attachments, agent names, and drag-and-drop state changes
- **Plan & Orchestrate**: LLM planner sequences Todo tasks (order, roles, subfolders, dependencies) then dispatches agents
- **Status indicators** on cards (queued / working / done) plus planned agent order numbers
- Copy completed tasks to the next iteration; add bugs on completed iterations

### Verification (QA)
- **Testing instructions** required before orchestrating a planning iteration
- After all build tasks reach a terminal state, an **adversarial QA agent** reviews the sprint against those instructions
- PASS completes the iteration; FAIL can reopen tasks to In Progress

### Operations
- Live orchestrator status, token usage, and runtime metrics
- **Run history** with iteration, task/bug kind, and per-attempt detail

### Artifacts
- Browse files agents created under each project workspace
- Preview and edit text files in the browser
- **Workspace console**: run shell commands in the project directory (login-shell environment)

### Git history
- When a project is git-enabled, view commits made after each sprint

### Model setup
- First launch prompts you to pick a Pi model (from your Pi configuration), test it, and save the choice
- Flight Deck stores the selection in `DATA/model_config.json` (local, not committed)

---

## How it uses Pi

Flight Deck talks to Pi through **RPC mode**, the same integration path described in the [Pi docs](https://pi.dev/) for non-Node callers:

| Piece | Role |
|-------|------|
| `WORKFLOW.md` → `codex.command` | Default: `pi --mode rpc` |
| `rpc_client.py` | JSONL protocol over stdin/stdout; one long-lived Pi session per task |
| `agent_runner.py` | Turn loop, verdict parsing (`STATUS: PASS` / `FAIL`), token sampling |
| `planner.py` | One-shot Pi RPC calls to sequence tasks before a sprint |
| `.pi/settings.json` | Shipped in this repo: loads the [LiteLLM provider extension](https://www.npmjs.com/package/pi-provider-litellm) for Pi (`npm:pi-provider-litellm`) |

Pi owns **models, API keys, OAuth, and tools**. Flight Deck inherits Pi’s login-shell environment when spawning agents and when running the Artifacts console.

**Credentials are not stored in this repository.** Configure them once in Pi on your machine (see below).

---

## Requirements

| Requirement | Notes |
|-------------|--------|
| **Python 3.10+** | Used by `./run.sh` to create `BACKEND/.venv` |
| **Pi** | Installed globally; must be on `PATH` as `pi` |
| **Node.js 22+** (recommended) | Required by Pi’s npm-based install and extensions |

Optional:
- **Linear** — set `tracker.kind: linear` in `WORKFLOW.md` and provide `LINEAR_API_KEY` (default tracker is built-in SQLite)
- **LiteLLM proxy** — if you use the shipped extension: `/login litellm` in Pi or `LITELLM_BASE_URL` + `LITELLM_API_KEY`

---

## Install Pi

Install Pi using any method from [pi.dev](https://pi.dev/):

```bash
# macOS / Linux
curl -fsSL https://pi.dev/install.sh | sh

# or npm
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
```

Verify:

```bash
pi --version
```

### Configure Pi (one time on your machine)

Pick **one** provider path:

**A — Direct provider (Anthropic, OpenAI, Google, etc.)**

```bash
pi
# then use /login in the TUI, or set the provider API key in your environment
```

**B — LiteLLM proxy (matches the shipped Flight Deck extension)**

```bash
export LITELLM_BASE_URL="https://your-litellm-host"
export LITELLM_API_KEY="sk-..."
# optional: authenticate in Pi
pi
/login litellm
```

Pi stores provider settings under `~/.pi/agent/`. Flight Deck reads your Pi default model and configured models from there during the dashboard model-setup step.

---

## Clone and run

```bash
git clone <your-repo-url> FlightDeck
cd FlightDeck
./run.sh
```

On first run, `./run.sh` will:

1. Create `BACKEND/.venv` and install Python dependencies from `requirements.txt`
2. Start the app (default dashboard: **http://127.0.0.1:8787/**)

Open that URL in a browser. Complete **Configure your model** (test connection → Save & continue), then create a project and start working.

### Optional flags

```bash
./run.sh --port 9000              # override dashboard port
./run.sh /path/to/WORKFLOW.md     # custom workflow file
ORCH_LOG_LEVEL=DEBUG ./run.sh    # verbose backend logs
```

---

## Database and local data

Flight Deck uses **SQLite** — a single file database, no separate database server.

| Path | Purpose |
|------|---------|
| `DATA/tracker.db` | Projects, iterations, tasks, dependencies, comments, run history |
| `DATA/model_config.json` | Your saved Pi model choice for Flight Deck |
| `WORKSPACES/` | Per-project agent workspaces (files agents create and edit) |
| `LOGS/backend/` | Backend log files |
| `LOGS/frontend/` | Browser/client log lines |

These paths are **created automatically** on first run and are **gitignored** — they stay on your machine.

Schema and migrations live in `BACKEND/db.py`.

---

## Project layout

```text
FlightDeck/
  run.sh                 # clone-and-run launcher
  README.md
  .pi/
    settings.json        # Pi extension list (LiteLLM provider); committed
  BACKEND/
    WORKFLOW.md          # tracker, agent, polling, prompt template
    main.py              # entry point
    orchestrator.py      # plan / orchestrate / QA / dispatch
    db.py                # SQLite tracker
    web.py               # Flask API + dashboard
    rpc_client.py        # Pi RPC client
    agent_runner.py      # per-task agent worker
    planner.py           # sprint planning via Pi
    pi_models.py         # Pi model discovery helpers
  FRONTEND/
    templates/dashboard.html
    static/app.js
    static/style.css
  DATA/                  # runtime (gitignored)
  LOGS/                  # runtime (gitignored)
  WORKSPACES/            # runtime (gitignored)
```

---

## Configuration

Edit `BACKEND/WORKFLOW.md` to change:

- **Tracker**: `kind: local` (default) or `kind: linear`
- **Concurrency**: `agent.max_concurrent_agents`
- **Turn limits and timeouts**
- **Agent prompt** (Jinja body below the YAML front matter)
- **Dashboard bind**: `server.host` / `server.port` (default `127.0.0.1:8787`)

For Linear, copy credentials into `BACKEND/.env`:

```bash
LINEAR_API_KEY=your_key_here
```

and set `tracker.kind: linear` plus `tracker.project_slug` in `WORKFLOW.md`.

---

## Typical workflow

1. **Install Pi** and configure a provider ([pi.dev](https://pi.dev/))
2. **Clone** this repo and run `./run.sh`
3. **Model setup** in the browser — test and save a model
4. **Create a project** on Mission Control
5. **Add an iteration**, tasks, and **testing instructions**
6. **Plan & Orchestrate** — planner orders tasks; agents run in `WORKSPACES/<project>/`
7. **QA** runs when build tasks finish; iteration completes on PASS
8. Review **Artifacts**, **Operations** run history, and optional **Git history**

---

## Troubleshooting

| Issue | What to check |
|-------|----------------|
| `pi not found on PATH` | Install Pi from [pi.dev](https://pi.dev/); restart shell |
| Model setup shows no models | Configure a provider in Pi (`/login` or env vars); set default model in Pi |
| Agents fail immediately | Run `pi --mode rpc` manually; verify API keys in `~/.pi/agent/` |
| Port 8787 in use | `./run.sh --port 8787` or change `server.port` in `WORKFLOW.md` |
| LiteLLM models empty | Set `LITELLM_BASE_URL` / `/login litellm`; Pi fetches the extension on first use |

Logs: `LOGS/backend/` and `LOGS/frontend/`.

---

## License

MIT (or your project license — update this line if needed).
