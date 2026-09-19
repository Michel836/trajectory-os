"""M036–M039 — explicit operator authorization for release Git writes.

This module is the *only* place an authorized release action is minted. The
release service never performs a commit/push/merge without an
:class:`ReleaseAuthorization` whose ``action`` matches the required gate and
whose ``token`` was supplied by the operator on the command line.

A token is deliberately a non-empty operator-supplied string (for example a
short-lived phrase). It is never read from the environment, never generated
automatically and never persisted: the durable evidence records only the
action, the actor, the timestamp and a SHA-256 *digest* of the token so the
release can be audited without storing the secret.

Design invariants:

* no implicit/default authorization — an absent, blank or mismatched token
  fails closed;
* implementation/review/runtime-control components can hold only an
  *unauthorized* sentinel and therefore can never mint a write;
* an authorization is bound to exactly one gate action.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from trajectory_os.release import model

#: Environment variable that would be a *forbidden* implicit authorization.
#: Authorization must always be explicit on the command; the environment is
#: never consulted (and this constant documents the prohibition).
FORBIDDEN_AUTHORIZATION_ENV = "TRAJECTORY_RELEASE_AUTHORIZE"

#: The sentinel actor used by non-operator components.
AGENT_ACTOR = "agent"


@dataclass(frozen=True)
class ReleaseAuthorization:
    """One explicit operator authorization for a single release gate."""

    action: str
    actor: str
    token_digest: str
    issued_at: str

    def validate(self) -> ReleaseAuthorization:
        if self.action not in model.RELEASE_GATES:
            raise model.ReleaseError(model.R_UNAUTHORIZED,
                                     f"unknown action {self.action!r}")
        if not self.actor:
            raise model.ReleaseError(model.R_UNAUTHORIZED, "actor required")
        if not model.is_sha256(self.token_digest):
            raise model.ReleaseError(model.R_UNAUTHORIZED,
                                     "authorization token digest required")
        return self

    def to_dict(self) -> dict[str, object]:
        """Safe durable view: the token itself is never exposed."""
        return {
            "action": self.action,
            "actor": self.actor,
            "token_digest": self.token_digest,
            "issued_at": self.issued_at,
            "explicit": True,
        }


def _digest(token: str) -> str:
    return hashlib.sha256(
        b"trajectory-os.release-authorization.v1\x00" + token.encode("utf-8")
    ).hexdigest()


def authorize(action: str, token: str | None, *,
              actor: str = "operator",
              clock: Callable[[], str] = model.utc_now) -> ReleaseAuthorization:
    """Mint an explicit authorization or fail closed (never implicit)."""
    if action not in model.RELEASE_GATES:
        raise model.ReleaseError(model.R_UNAUTHORIZED,
                                 f"unknown release gate {action!r}")
    if not isinstance(token, str) or not token.strip():
        raise model.ReleaseError(
            model.R_UNAUTHORIZED,
            f"explicit --authorize-{_flag_name(action)} token required")
    if not actor or actor == AGENT_ACTOR:
        raise model.ReleaseError(
            model.R_UNAUTHORIZED,
            "release Git writes require an explicit operator actor")
    return ReleaseAuthorization(
        action=action, actor=actor, token_digest=_digest(token.strip()),
        issued_at=clock()).validate()


def _flag_name(action: str) -> str:
    return "commit" if action == model.GATE_GO_COMMIT else "merge"


def unauthorized(action: str, *, actor: str = AGENT_ACTOR,
                 clock: Callable[[], str] = model.utc_now) -> ReleaseAuthorization:
    """Build a deliberately invalid sentinel (fails validation).

    Non-operator components may construct this to prove that they cannot
    mint a usable authorization; it always fails :meth:`validate`.
    """
    return ReleaseAuthorization(
        action=action, actor=actor,
        token_digest="",
        issued_at=clock())


__all__ = [
    "AGENT_ACTOR",
    "FORBIDDEN_AUTHORIZATION_ENV",
    "ReleaseAuthorization",
    "authorize",
    "unauthorized",
]
