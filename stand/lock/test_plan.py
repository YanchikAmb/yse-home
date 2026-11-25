# -*- coding: utf-8 -*-
import unittest

from lock_speaker import plan


class PlanTest(unittest.TestCase):
    def test_skip_and_switch(self):
        self.assertEqual(plan("192.168.209.105", "v", True, {}, {"192.168.209.105"}, False), "skip")
        self.assertEqual(plan("192.168.209.101", "v", False, {}, set(), True), "skip")

    def test_done_only_when_ssh_and_same_version(self):
        state = {"state": "done", "version": "0.369"}
        self.assertEqual(plan("192.168.209.101", "0.369", True, state, set(), False), "done")
        self.assertEqual(plan("192.168.209.101", "0.372", True, state, set(), False), "check")
        self.assertEqual(plan("192.168.209.101", "0.369", False, state, set(), False), "lock")

    def test_open_ssh_without_record_is_checked(self):
        self.assertEqual(plan("192.168.209.101", "0.369", True, None, set(), False), "check")

    def test_closed_ssh_locks(self):
        self.assertEqual(plan("192.168.209.120", "0.369", False, None, set(), False), "lock")


if __name__ == "__main__":
    unittest.main()
