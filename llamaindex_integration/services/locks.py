# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Redis lock for the create stage (spec section 6, step 5).

A lock is one redis key, set with NX and an expiry. Its value is a random token, so a release deletes the
key only while the key still holds this acquisition's token. The key lives in frappe.cache(), which every
worker on the bench shares. The key includes the site name, so sites on one bench do not block each other.
The lock is not a database lock, so it does not hold a transaction open.
"""

import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import frappe
import redis

KEY_PREFIX = "llama-lock:"
TOKEN_BYTES = 16
# Compare-and-delete: the key is removed only when it still holds this acquisition's token.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
	return redis.call('del', KEYS[1])
end
return 0
"""


class LockTimeoutError(Exception):
	"""The lock was still held when the wait time ran out, or Redis was unavailable while acquiring it."""


def lock_key(name: str) -> str:
	"""Return the redis key for the lock called name on the current site, "llama-lock:<site>:<name>".

	The site is part of the key, so the same lock name on two sites does not block between them.
	"""
	return f"{KEY_PREFIX}{frappe.local.site}:{name}"


@contextmanager
def create_lock(name: str, *, ttl_s: int = 300, wait_s: int = 120, poll_s: float = 0.5) -> Iterator[None]:
	"""Hold the lock called name for the body of the with block.

	Polls every poll_s seconds until the key is set, and raises LockTimeoutError once wait_s seconds have
	passed. wait_s=0 makes one attempt. The key expires after ttl_s seconds, so a worker that dies while
	holding the lock does not block the name forever.

	A redis ConnectionError or TimeoutError during acquisition is raised as LockTimeoutError at once, with a
	message that says Redis was unavailable. The redis error text is left out of the message and the chain, so
	no connection detail reaches a stored traceback. The caller treats it like a timeout and retries once.
	Release never raises, and never replaces an exception raised by the body.
	"""
	client = frappe.cache()
	key = lock_key(name)
	token = secrets.token_hex(TOKEN_BYTES)
	_acquire(client, name, key, token, ttl_s=ttl_s, wait_s=wait_s, poll_s=poll_s)
	try:
		yield
	finally:
		_release(client, key, token)


def _acquire(
	client: Any, name: str, key: str, token: str, *, ttl_s: int, wait_s: float, poll_s: float
) -> None:
	deadline = time.monotonic() + wait_s
	while True:
		try:
			acquired = client.set(key, token, nx=True, ex=ttl_s)
		except (redis.ConnectionError, redis.TimeoutError) as exc:
			# Only the exception class is logged. "from None" keeps the redis error out of the traceback.
			frappe.logger("llamaindex_integration").warning(
				f"lock acquire failed key={key} error={type(exc).__name__}"
			)
			raise LockTimeoutError(
				f"Lock {name} could not be acquired because Redis was unavailable."
			) from None
		if acquired:
			return
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			raise LockTimeoutError(f"Lock {name} is still held after {wait_s}s.")
		time.sleep(min(poll_s, remaining))


def _release(client: Any, key: str, token: str) -> None:
	try:
		client.register_script(_RELEASE_SCRIPT)(keys=[key], args=[token])
	except Exception as exc:
		# Only the exception class is logged. The key is kept, so the lock expires after its ttl.
		frappe.logger("llamaindex_integration").warning(
			f"lock release failed key={key} error={type(exc).__name__}"
		)
