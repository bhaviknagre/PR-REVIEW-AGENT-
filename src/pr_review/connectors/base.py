"""Code-host connector interface; GitHub is the only v1 implementation (FR-5)."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pr_review.connectors.github.client import PullRequest


@dataclass(frozen=True)
class PullRequestRef:
    installation_id: int
    repo: str  # "owner/name"
    number: int


@dataclass(frozen=True)
class CloneCredential:
    """Short-lived clone credential. Never persist; pass via env/askpass, not remote URLs."""

    username: str
    token: str

    def __repr__(self) -> str:
        return f"CloneCredential(username={self.username!r}, token='***')"

    __str__ = __repr__


class Connector(Protocol):
    def verify_event(self, headers: Mapping[str, str], body: bytes) -> bool: ...

    async def fetch_pull_request(self, pr: PullRequestRef) -> "PullRequest": ...

    async def fetch_diff(self, pr: PullRequestRef) -> str: ...

    async def head_sha(self, pr: PullRequestRef) -> str: ...

    async def clone_credential(self, pr: PullRequestRef) -> CloneCredential: ...

    async def post_review(
        self, pr: PullRequestRef, commit_sha: str, body: str, comments: list[dict[str, Any]]
    ) -> int: ...
