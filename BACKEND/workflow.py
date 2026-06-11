from __future__ import annotations

from pathlib import Path

import yaml


class WorkflowError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class WorkflowDefinition:
    def __init__(self, config: dict, prompt_template: str, path: Path):
        self.config = config
        self.prompt_template = prompt_template
        self.path = path

    @property
    def dir(self) -> Path:
        return self.path.parent


def load_workflow(path: Path) -> WorkflowDefinition:
    """Parse a WORKFLOW.md file into front-matter config + prompt body (SPEC 5.2)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError("missing_workflow_file", f"cannot read {path}: {exc}")

    config: dict = {}
    body = text

    if text.startswith("---"):
        lines = text.splitlines()
        end_index = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_index = i
                break
        if end_index is None:
            raise WorkflowError(
                "workflow_parse_error", "front matter opened with --- but never closed"
            )
        front_matter = "\n".join(lines[1:end_index])
        body = "\n".join(lines[end_index + 1 :])
        try:
            parsed = yaml.safe_load(front_matter) if front_matter.strip() else {}
        except yaml.YAMLError as exc:
            raise WorkflowError("workflow_parse_error", f"invalid YAML front matter: {exc}")
        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict):
            raise WorkflowError(
                "workflow_front_matter_not_a_map", "front matter must decode to a map"
            )
        config = parsed

    return WorkflowDefinition(config=config, prompt_template=body.strip(), path=path)
