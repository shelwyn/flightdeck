from __future__ import annotations

from typing import Optional

import requests

from models import Blocker, Issue
from tracker import TrackerError

_TIMEOUT = 30
_PAGE_SIZE = 50

_ISSUE_FIELDS = """
  id
  identifier
  title
  description
  priority
  url
  branchName
  createdAt
  updatedAt
  state { name }
  labels { nodes { name } }
  inverseRelations(first: 50) {
    nodes { type issue { id identifier state { name } } }
  }
"""

_CANDIDATES_QUERY = """
query Candidates($slug: String!, $states: [String!], $after: String) {
  issues(
    first: %d
    after: $after
    filter: { project: { slugId: { eq: $slug } }, state: { name: { in: $states } } }
  ) {
    nodes { %s }
    pageInfo { hasNextPage endCursor }
  }
}
""" % (_PAGE_SIZE, _ISSUE_FIELDS)

_BY_STATES_QUERY = """
query ByStates($slug: String!, $states: [String!], $after: String) {
  issues(
    first: %d
    after: $after
    filter: { project: { slugId: { eq: $slug } }, state: { name: { in: $states } } }
  ) {
    nodes { id identifier state { name } }
    pageInfo { hasNextPage endCursor }
  }
}
""" % _PAGE_SIZE

_BY_IDS_QUERY = """
query ByIds($ids: [ID!]) {
  issues(filter: { id: { in: $ids } }) {
    nodes { %s }
  }
}
""" % _ISSUE_FIELDS

_TEAM_STATES_QUERY = """
query IssueTeamStates($id: String!) {
  issue(id: $id) {
    id
    team { states { nodes { id name } } }
  }
}
"""

_COMMENT_MUTATION = """
mutation AddComment($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) { success }
}
"""

_STATE_MUTATION = """
mutation SetState($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: { stateId: $stateId }) { success }
}
"""


class LinearError(TrackerError):
    pass


def _normalize_issue(node: dict) -> Issue:
    blockers: list[Blocker] = []
    inverse = (node.get("inverseRelations") or {}).get("nodes") or []
    for rel in inverse:
        if (rel.get("type") or "").lower() != "blocks":
            continue
        blocker = rel.get("issue") or {}
        blockers.append(
            Blocker(
                id=blocker.get("id"),
                identifier=blocker.get("identifier"),
                state=((blocker.get("state") or {}).get("name")),
            )
        )

    labels = [
        str(item.get("name")).strip().lower()
        for item in ((node.get("labels") or {}).get("nodes") or [])
        if item.get("name")
    ]

    priority = node.get("priority")
    if not isinstance(priority, int):
        priority = None

    return Issue(
        id=node.get("id"),
        identifier=node.get("identifier"),
        title=node.get("title") or "",
        state=((node.get("state") or {}).get("name") or ""),
        description=node.get("description"),
        priority=priority,
        branch_name=node.get("branchName"),
        url=node.get("url"),
        labels=labels,
        blocked_by=blockers,
        created_at=node.get("createdAt"),
        updated_at=node.get("updatedAt"),
    )


class LinearClient:
    def __init__(self, endpoint: str, api_key: str, project_slug: str):
        self.endpoint = endpoint
        self.api_key = api_key
        self.project_slug = project_slug
        self._session = requests.Session()

    def _execute(self, query: str, variables: dict) -> dict:
        try:
            response = self._session.post(
                self.endpoint,
                json={"query": query, "variables": variables},
                headers={"Authorization": self.api_key, "Content-Type": "application/json"},
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise LinearError("linear_api_request", str(exc))

        if response.status_code != 200:
            raise LinearError(
                "linear_api_status", f"HTTP {response.status_code}: {response.text[:300]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise LinearError("linear_unknown_payload", str(exc))

        if payload.get("errors"):
            raise LinearError("linear_graphql_errors", str(payload["errors"]))

        data = payload.get("data")
        if data is None:
            raise LinearError("linear_unknown_payload", "missing data in response")
        return data

    def _paginate_issues(self, query: str, states: list[str]) -> list[dict]:
        nodes: list[dict] = []
        after: Optional[str] = None
        while True:
            data = self._execute(
                query,
                {"slug": self.project_slug, "states": states, "after": after},
            )
            block = data.get("issues") or {}
            nodes.extend(block.get("nodes") or [])
            page = block.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
            if not after:
                raise LinearError(
                    "linear_missing_end_cursor", "hasNextPage true but endCursor missing"
                )
        return nodes

    def fetch_candidate_issues(self, active_states: list[str]) -> list[Issue]:
        nodes = self._paginate_issues(_CANDIDATES_QUERY, active_states)
        return [_normalize_issue(node) for node in nodes]

    def fetch_issues_by_states(self, states: list[str]) -> list[Issue]:
        if not states:
            return []
        nodes = self._paginate_issues(_BY_STATES_QUERY, states)
        return [
            Issue(
                id=n.get("id"),
                identifier=n.get("identifier"),
                title="",
                state=((n.get("state") or {}).get("name") or ""),
            )
            for n in nodes
        ]

    def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        if not issue_ids:
            return []
        data = self._execute(_BY_IDS_QUERY, {"ids": issue_ids})
        nodes = (data.get("issues") or {}).get("nodes") or []
        return [_normalize_issue(node) for node in nodes]

    # --- writes (orchestrator-owned, SPEC 11.5 deviation) ---
    def add_comment(self, issue_id: str, body: str) -> bool:
        data = self._execute(_COMMENT_MUTATION, {"issueId": issue_id, "body": body})
        return bool((data.get("commentCreate") or {}).get("success"))

    def set_issue_state(self, issue_id: str, state_name: str) -> bool:
        data = self._execute(_TEAM_STATES_QUERY, {"id": issue_id})
        states = (((data.get("issue") or {}).get("team") or {}).get("states") or {}).get(
            "nodes"
        ) or []
        target = next(
            (s for s in states if (s.get("name") or "").strip().lower() == state_name.strip().lower()),
            None,
        )
        if not target:
            raise LinearError(
                "linear_unknown_payload", f"no workflow state named {state_name!r}"
            )
        result = self._execute(
            _STATE_MUTATION, {"id": issue_id, "stateId": target["id"]}
        )
        return bool((result.get("issueUpdate") or {}).get("success"))
