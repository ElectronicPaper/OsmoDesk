"""Expiring, revocable local crew credentials; never a public identity service."""
from __future__ import annotations

import hashlib
import secrets
import threading
import time

EDITOR_POSTS = frozenset({
    '/api/move', '/api/move/retime', '/api/move/offset', '/api/move/reference',
    '/api/moves/save', '/api/moves/load', '/api/moves/delete', '/api/setup',
    '/api/slate', '/api/take', '/api/take/circle', '/api/director/preview',
    '/api/takes/compare', '/api/timelapse/plan', '/api/move/export',
    '/api/editorial/export',
})
HOST_ROUTES = frozenset({'/api/settings/ai', '/api/crew', '/api/crew/revoke',
                          '/api/workspace/recovery'})


class CrewAccess:
    """Tokens are returned once; only hashes live in this process."""
    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.RLock()
        self._members = {}

    def issue(self, *, label='', role='viewer', hours=4, allow_ai=False):
        if role not in ('viewer', 'editor', 'operator'):
            raise ValueError('Choose a viewer, editor or operator role')
        if not isinstance(label, str) or len(label) > 64 or any(ord(c) < 32 for c in label):
            raise ValueError('Crew label must be at most 64 printable characters')
        if type(hours) not in (int, float) or not 0.25 <= hours <= 24:
            raise ValueError('Crew access must expire within 15 minutes to 24 hours')
        if type(allow_ai) is not bool or (role == 'viewer' and allow_ai):
            raise ValueError('AI access requires an editor or operator role')
        with self._lock:
            self._prune()
            if len(self._members) >= 32:
                raise ValueError('Revoke a crew session before adding another')
            token = secrets.token_urlsafe(32)
            ident = secrets.token_hex(8)
            member = dict(id=ident, label=label.strip() or role.title(), role=role,
                          allow_ai=allow_ai, expires_at=self._clock() + hours * 3600)
            self._members[ident] = (hashlib.sha256(token.encode()).digest(), member)
            return dict(member, token=token)

    def _prune(self):
        now = self._clock()
        for ident, (_, member) in list(self._members.items()):
            if member['expires_at'] <= now:
                del self._members[ident]

    def resolve(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 256:
            return None
        hashed = hashlib.sha256(token.encode()).digest()
        with self._lock:
            self._prune()
            for stored, member in self._members.values():
                if secrets.compare_digest(stored, hashed):
                    return dict(member)
        return None

    def list(self):
        with self._lock:
            self._prune()
            return [dict(member) for _, member in self._members.values()]

    def revoke(self, ident):
        with self._lock:
            if not isinstance(ident, str) or ident not in self._members:
                raise ValueError('Crew session is missing or expired')
            del self._members[ident]


def permitted(principal, method, route):
    if principal is None:
        return False
    role = principal['role']
    if route in HOST_ROUTES:
        return role == 'owner'
    if route.startswith('/api/assistant/'):
        return role == 'owner' or bool(principal.get('allow_ai'))
    if method == 'GET':
        return True  # Existing GET surface is read-only; static allowlist lives in Handler.
    if role in ('owner', 'operator'):
        return True  # Existing dispatch is the canonical operation allowlist.
    return role == 'editor' and route in EDITOR_POSTS
