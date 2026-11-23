# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Tests for services/locks.py: acquire and release, blocking while held, LockTimeoutError, and the
compare-and-delete release that leaves a key alone once its token has changed.

The lock lives in frappe.cache(), so these tests use the real redis cache. Each test takes its own lock
name and removes its key afterwards.
"""

from unittest.mock import patch

import frappe
import redis
from frappe.tests import IntegrationTestCase

from llamaindex_integration.services.locks import LockTimeoutError, create_lock, lock_key


class TestLockKey(IntegrationTestCase):
	def test_lock_key_includes_the_site_name(self):
		site = frappe.local.site
		self.assertEqual(lock_key("ToDo"), f"llama-lock:{site}:ToDo")
		frappe.local.site = "other-site.test"
		try:
			self.assertEqual(lock_key("ToDo"), "llama-lock:other-site.test:ToDo")
		finally:
			frappe.local.site = site


class TestCreateLock(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.name = f"test-{frappe.generate_hash(length=10)}"
		self.key = lock_key(self.name)

	def tearDown(self):
		frappe.cache().delete(self.key)

	def test_acquire_sets_key_and_release_deletes_it(self):
		with create_lock(self.name):
			self.assertIsNotNone(frappe.cache().get(self.key))
		self.assertIsNone(frappe.cache().get(self.key))

	def test_lock_can_be_acquired_again_after_release(self):
		with create_lock(self.name):
			pass
		with create_lock(self.name, wait_s=0):
			self.assertIsNotNone(frappe.cache().get(self.key))
		self.assertIsNone(frappe.cache().get(self.key))

	def test_second_acquire_is_blocked_while_first_is_held(self):
		with create_lock(self.name), self.assertRaises(LockTimeoutError), create_lock(self.name, wait_s=0):
			self.fail("the second acquire must not succeed while the first is held")

	def test_second_acquire_times_out_after_its_wait(self):
		with (
			create_lock(self.name),
			self.assertRaises(LockTimeoutError),
			create_lock(self.name, wait_s=1, poll_s=0.1),
		):
			self.fail("the second acquire must not succeed while the first is held")

	def test_release_keeps_key_whose_token_changed(self):
		with create_lock(self.name):
			frappe.cache().set(self.key, "another-holder")
		self.assertEqual(frappe.cache().get(self.key), b"another-holder")

	def test_release_error_does_not_mask_the_body_exception(self):
		failing_release = patch.object(
			frappe.cache(), "register_script", side_effect=redis.ConnectionError("down")
		)
		with failing_release, self.assertRaises(ValueError), create_lock(self.name):
			raise ValueError("body failed")

	def test_release_error_is_not_raised_on_normal_exit(self):
		failing_release = patch.object(
			frappe.cache(), "register_script", side_effect=redis.ConnectionError("down")
		)
		with failing_release, create_lock(self.name):
			pass

	def test_redis_error_during_acquire_raises_lock_timeout_without_waiting(self):
		for error in (redis.ConnectionError("secret-host-detail"), redis.TimeoutError("secret-host-detail")):
			with self.subTest(error=type(error).__name__):
				with (
					patch.object(frappe.cache(), "set", side_effect=error) as set_key,
					self.assertRaises(LockTimeoutError) as raised,
					create_lock(self.name, wait_s=5),
				):
					self.fail("the body must not run without the lock")

				self.assertEqual(set_key.call_count, 1)
				self.assertIn("Redis was unavailable", str(raised.exception))
				self.assertNotIn("secret-host-detail", str(raised.exception))
				self.assertIsNone(raised.exception.__cause__)
				self.assertTrue(raised.exception.__suppress_context__)
