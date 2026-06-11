from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from config import ConfigManager
from logging_setup import configure_logging, get_logger
from orchestrator import Orchestrator
from workflow import WorkflowError

log = get_logger("orch.cli")

# Ignore the first SIGTERM while work is in flight. Agents often run port-cleanup
# commands (lsof/fuser/kill) that accidentally signal this process too.
_sigterm_pending = False


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path.cwd() / ".env")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flightdeck", description="Flight Deck — tracker-driven Pi coding-agent orchestrator")
    parser.add_argument(
        "workflow",
        nargs="?",
        default="./WORKFLOW.md",
        help="Path to WORKFLOW.md (default: ./WORKFLOW.md)",
    )
    parser.add_argument("--log-level", default=os.environ.get("ORCH_LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP dashboard port (overrides server.port in WORKFLOW.md)",
    )
    args = parser.parse_args(argv)

    configure_logging(args.log_level)
    _load_dotenv()

    workflow_path = Path(args.workflow).expanduser().resolve()
    if not workflow_path.is_file():
        log.error("workflow file not found: %s", workflow_path)
        return 1

    try:
        config_manager = ConfigManager(workflow_path)
    except WorkflowError as exc:
        log.error("failed to load workflow: %s", exc)
        return 1

    orchestrator = Orchestrator(config_manager, port=args.port)

    def handle_signal(signum, _frame):
        global _sigterm_pending
        if not orchestrator.is_active():
            _sigterm_pending = False
        if signum == signal.SIGTERM and orchestrator.is_active() and not _sigterm_pending:
            _sigterm_pending = True
            log.warning(
                "received SIGTERM during active work (pid=%s ppid=%s); ignoring. "
                "This often happens when an agent runs port-cleanup (lsof/fuser/kill). "
                "Send SIGTERM again or press Ctrl+C to stop Flight Deck.",
                os.getpid(),
                os.getppid(),
            )
            return
        log.info("received signal %s, stopping pid=%s", signum, os.getpid())
        orchestrator.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    return orchestrator.run()


if __name__ == "__main__":
    sys.exit(main())
