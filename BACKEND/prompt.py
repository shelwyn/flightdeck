from __future__ import annotations

from typing import Optional

from jinja2 import Environment, StrictUndefined
from jinja2 import TemplateError as JinjaTemplateError

from models import Issue


class PromptError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


_env = Environment(undefined=StrictUndefined, autoescape=False)


def render_prompt(template: str, issue: Issue, attempt: Optional[int]) -> str:
    """Strictly render the workflow prompt body (SPEC 5.4 / 12.2)."""
    if not template.strip():
        return "You are working on a task in this project."
    try:
        compiled = _env.from_string(template)
    except JinjaTemplateError as exc:
        raise PromptError("template_parse_error", str(exc))
    try:
        return compiled.render(issue=issue.to_template_dict(), attempt=attempt)
    except JinjaTemplateError as exc:
        raise PromptError("template_render_error", str(exc))
