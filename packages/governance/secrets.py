"""Secret store and egress leak scanning.

Two jobs, deliberately in one module because they are two halves of one rule:
ANA holds the secrets an agent may need (project- or global-scoped), and the
gate refuses any tool call that would carry one of those values out of the
machine. A store without the scan is just a config file; a scan without the
store has nothing to look for.

Values are encrypted at rest with a key derived from ANA_SECRET_KEY. This is
not protection against someone who already has the server's environment — it
is protection against the database file, a backup or a `SELECT *` leaking
every credential in plain text.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets as secrets_module
from dataclasses import dataclass
from typing import Optional

from psycopg_pool import AsyncConnectionPool

GLOBAL_SCOPE = "global"

SECRET_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS secrets (
    id BIGSERIAL PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT 'global',
    name TEXT NOT NULL,
    value_encrypted TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, name)
);
"""

# A value shorter than this is not treated as a secret for scanning purposes:
# short strings ("1", "dev", "true") appear in ordinary commands constantly
# and would block everything.
MIN_SCANNABLE_LENGTH = 8


class SecretCipher:
    """Keystream cipher over HMAC-SHA256, with a per-value nonce.

    Chosen over a third-party library only to avoid adding a dependency for
    this slice; the trade-off is that it provides confidentiality at rest, not
    authenticated encryption. The key comes from ANA_SECRET_KEY; without it a
    process-lifetime random key is used, which makes stored values unreadable
    after a restart — loudly wrong rather than quietly insecure."""

    def __init__(self, key: Optional[str] = None):
        raw = key or os.environ.get("ANA_SECRET_KEY") or secrets_module.token_hex(32)
        self.key = hashlib.sha256(raw.encode("utf-8")).digest()

    def _keystream(self, nonce: bytes, length: int) -> bytes:
        stream = b""
        counter = 0
        while len(stream) < length:
            stream += hmac.new(
                self.key, nonce + counter.to_bytes(4, "big"), hashlib.sha256
            ).digest()
            counter += 1
        return stream[:length]

    def _tag(self, nonce: bytes, ciphertext: bytes) -> bytes:
        return hmac.new(self.key, b"tag" + nonce + ciphertext, hashlib.sha256).digest()

    def encrypt(self, plaintext: str) -> str:
        nonce = secrets_module.token_bytes(16)
        data = plaintext.encode("utf-8")
        ciphertext = bytes(a ^ b for a, b in zip(data, self._keystream(nonce, len(data))))
        # Tag over the ciphertext, so a wrong key or a tampered row is detected
        # outright. Without it, decrypting under the wrong key returns garbage
        # that occasionally happens to be valid UTF-8 — a silently wrong
        # credential rather than an error.
        return base64.b64encode(nonce + self._tag(nonce, ciphertext) + ciphertext).decode(
            "ascii"
        )

    def decrypt(self, stored: str) -> str:
        blob = base64.b64decode(stored.encode("ascii"))
        nonce, tag, ciphertext = blob[:16], blob[16:48], blob[48:]
        if not hmac.compare_digest(tag, self._tag(nonce, ciphertext)):
            # Almost always a changed or missing ANA_SECRET_KEY. Say that,
            # rather than surfacing a decoding error nobody can act on.
            raise ValueError(
                "stored secret could not be decrypted; ANA_SECRET_KEY has changed or is unset"
            )
        data = bytes(
            a ^ b for a, b in zip(ciphertext, self._keystream(nonce, len(ciphertext)))
        )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("stored secret is not valid UTF-8") from exc


@dataclass(frozen=True)
class Secret:
    scope: str
    name: str
    value: str


async def run_secret_migrations(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute(SECRET_MIGRATIONS)


async def put_secret(
    pool: AsyncConnectionPool, cipher: SecretCipher, *, scope: str, name: str, value: str
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO secrets (scope, name, value_encrypted)
            VALUES (%s, %s, %s)
            ON CONFLICT (scope, name)
            DO UPDATE SET value_encrypted = EXCLUDED.value_encrypted, updated_at = now()
            """,
            (scope, name, cipher.encrypt(value)),
        )


async def delete_secret(pool: AsyncConnectionPool, *, scope: str, name: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM secrets WHERE scope = %s AND name = %s", (scope, name)
        )
        return cur.rowcount > 0


async def list_secrets(pool: AsyncConnectionPool, *, scope: Optional[str] = None) -> list[dict]:
    """Names and scopes only — never values. What the management UI shows."""
    query = "SELECT scope, name, updated_at FROM secrets"
    params: tuple = ()
    if scope is not None:
        query += " WHERE scope = %s"
        params = (scope,)
    query += " ORDER BY scope, name"
    async with pool.connection() as conn:
        cur = await conn.execute(query, params)
        rows = await cur.fetchall()
    return [{"scope": r[0], "name": r[1], "updated_at": r[2].isoformat()} for r in rows]


async def load_secrets(
    pool: AsyncConnectionPool, cipher: SecretCipher, *, scope: Optional[str] = None
) -> list[Secret]:
    """Decrypted secrets for injection and for leak scanning. Global-scoped
    ones are always included: a project scope narrows what is added, never
    what is protected."""
    query = "SELECT scope, name, value_encrypted FROM secrets"
    params: tuple = ()
    if scope is not None:
        query += " WHERE scope = %s OR scope = %s"
        params = (scope, GLOBAL_SCOPE)
    async with pool.connection() as conn:
        cur = await conn.execute(query, params)
        rows = await cur.fetchall()

    loaded = []
    for scope_value, name, encrypted in rows:
        try:
            loaded.append(Secret(scope=scope_value, name=name, value=cipher.decrypt(encrypted)))
        except ValueError:
            # A row written under a different ANA_SECRET_KEY. Skipped rather
            # than raised: one stale row must not break every tool call the
            # gate screens. It still shows in list_secrets(), so the operator
            # can re-enter or delete it.
            continue
    return loaded


def scan_for_secrets(text: str, secrets: list[Secret]) -> list[str]:
    """Names of the secrets whose *value* appears in `text`.

    Matching is on the value, not the name: a command that prints
    `$GITHUB_TOKEN` is fine, one that carries the token itself is the leak.
    Very short values are skipped (see MIN_SCANNABLE_LENGTH) because they
    match everything and would make the gate useless."""
    if not text:
        return []
    found = []
    for secret in secrets:
        if len(secret.value) < MIN_SCANNABLE_LENGTH:
            continue
        if secret.value in text:
            found.append(secret.name)
    return found


def mask(text: str, secrets: list[Secret]) -> str:
    """Replaces secret values with a marker, for anything that gets stored or
    displayed (audit rows, approval cards)."""
    for secret in secrets:
        if len(secret.value) >= MIN_SCANNABLE_LENGTH and secret.value in text:
            text = text.replace(secret.value, f"[{secret.name} 마스킹됨]")
    return text


_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def valid_secret_name(name: str) -> bool:
    """Names become environment variables in the agent subprocess, so they
    must look like environment variables — anything else could smuggle extra
    entries into the environment."""
    return bool(_NAME_RE.match(name or ""))
