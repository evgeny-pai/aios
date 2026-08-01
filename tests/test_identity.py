"""Who this machine is, as a name a human can read.

`node_id` was already covered by nothing at all, which is how a three-node mesh ended
up telling its nodes apart by eye. These are the assertions that make the name usable:
it is stable across boots, it is different on different machines, and it carries the
generation — the one part that changes and the part that has actually misled.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aios import identity


class TestNodeName(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / ".aios").mkdir()

    def test_a_name_is_minted_once_and_then_kept(self):
        first = identity.node_name(self.dir)
        self.assertEqual(first, identity.node_name(self.dir), "a machine may not rename itself")
        self.assertEqual(
            (self.dir / ".aios" / identity.NAME_FILE).read_text(encoding="utf-8").strip(),
            first,
            "the name has to be on the state volume or a recreate loses it",
        )

    def test_the_name_is_a_word_and_a_suffix(self):
        name = identity.node_name(self.dir)
        word, _, suffix = name.rpartition("-")
        self.assertIn(word, identity.WORDS)
        self.assertRegex(suffix, r"^[0-9a-f]{4}$")

    def test_two_machines_get_different_names(self):
        other = Path(tempfile.mkdtemp())
        (other / ".aios").mkdir()
        # Not a strict inequality assertion on one draw — 16 words x 16 bits collide
        # occasionally, and a test that fails once a month is worse than no test. What
        # must hold is that the name comes from the machine's own state, not a constant.
        names = set()
        for _ in range(8):
            root = Path(tempfile.mkdtemp())
            (root / ".aios").mkdir()
            names.add(identity.node_name(root))
        self.assertGreater(len(names), 1, "every machine minted the same name")
        self.assertNotEqual(identity.node_name(other), "", "a fresh machine still gets a name")

    def test_an_unwritable_state_volume_still_yields_a_name(self):
        """Persistence is a volume's job; being someone is not conditional on it."""
        with mock.patch.object(Path, "replace", side_effect=OSError("read-only")):
            self.assertTrue(identity.node_name(Path(tempfile.mkdtemp())))


class TestEnsureAccount(unittest.TestCase):
    """The account the cockpit's shell pane drops to: wheel, plus the sudoers drop-in.

    Everything ensure_account makes lives in the ephemeral layer, so it runs at every
    boot and must CONVERGE: create what is missing, leave alone what is right, and
    never take the boot down with it. These tests pin the pieces a recreate erases —
    an account in wheel and a 0440 drop-in whose content is exactly the one grant.
    """

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.sudoers = self.dir / "sudoers.d" / "aios-operator"
        patcher = mock.patch.object(identity, "SUDOERS", self.sudoers)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _wheel(self, members=()):
        return mock.Mock(gr_mem=list(members))

    def test_a_missing_account_is_created_and_joined_to_wheel(self):
        calls = []
        with mock.patch.object(identity.pwd, "getpwnam", side_effect=KeyError), \
             mock.patch.object(identity.grp, "getgrnam", return_value=self._wheel()), \
             mock.patch.object(identity.subprocess, "run",
                               side_effect=lambda argv, **kw: calls.append(argv)):
            self.assertTrue(identity.ensure_account("anvil-9f2c"))
        self.assertEqual(calls[0][:1], ["useradd"])
        self.assertIn("--create-home", calls[0])
        self.assertEqual(calls[1][:3], ["usermod", "-aG", identity.WHEEL])

    def test_an_existing_account_in_wheel_costs_no_subprocess(self):
        """Boot and login both call this; the steady state has to be free."""
        with mock.patch.object(identity.pwd, "getpwnam", return_value=object()), \
             mock.patch.object(identity.grp, "getgrnam",
                               return_value=self._wheel(["anvil-9f2c"])), \
             mock.patch.object(identity.subprocess, "run") as run:
            self.assertTrue(identity.ensure_account("anvil-9f2c"))
        run.assert_not_called()

    def test_the_drop_in_is_the_one_grant_at_the_mode_sudo_accepts(self):
        with mock.patch.object(identity.pwd, "getpwnam", return_value=object()), \
             mock.patch.object(identity.grp, "getgrnam",
                               return_value=self._wheel(["anvil-9f2c"])):
            identity.ensure_account("anvil-9f2c")
        self.assertEqual(self.sudoers.read_text(encoding="utf-8"),
                         "%wheel ALL=(ALL:ALL) NOPASSWD: ALL\n")
        self.assertEqual(self.sudoers.stat().st_mode & 0o777, 0o440,
                         "sudo refuses a sudoers file that is writable by others")
        # Nothing else may be in the directory: a leftover temp file with a dot in
        # its name is skipped by sudoers.d rule, but a second grant would not be.
        self.assertEqual([p.name for p in self.sudoers.parent.iterdir()],
                         ["aios-operator"])

    def test_a_correct_drop_in_is_left_alone_and_a_wrong_one_is_replaced(self):
        self.sudoers.parent.mkdir(parents=True)
        self.sudoers.write_text(identity.SUDOERS_TEXT, encoding="utf-8")
        self.sudoers.chmod(0o440)
        before = self.sudoers.stat().st_ino
        with mock.patch.object(identity.pwd, "getpwnam", return_value=object()), \
             mock.patch.object(identity.grp, "getgrnam",
                               return_value=self._wheel(["anvil-9f2c"])):
            identity.ensure_account("anvil-9f2c")
            self.assertEqual(self.sudoers.stat().st_ino, before,
                             "a converged file must not be rewritten every boot")
            self.sudoers.chmod(0o644)  # a hand edit widened it
            identity.ensure_account("anvil-9f2c")
        self.assertEqual(self.sudoers.stat().st_mode & 0o777, 0o440)

    def test_a_failed_useradd_grants_nothing(self):
        """No account means no privilege file: half an identity is worse than none."""
        with mock.patch.object(identity.pwd, "getpwnam", side_effect=KeyError), \
             mock.patch.object(identity.subprocess, "run",
                               side_effect=OSError("no useradd")):
            self.assertFalse(identity.ensure_account("anvil-9f2c"))
        self.assertFalse(self.sudoers.exists())

    def test_a_userland_with_no_wheel_group_still_yields_the_account(self):
        with mock.patch.object(identity.pwd, "getpwnam", return_value=object()), \
             mock.patch.object(identity.grp, "getgrnam", side_effect=KeyError), \
             mock.patch.object(identity.subprocess, "run") as run:
            self.assertTrue(identity.ensure_account("anvil-9f2c"))
        run.assert_not_called()


class TestHostname(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / ".aios").mkdir()

    def test_the_hostname_is_aios_generation_name(self):
        host = identity.hostname(self.dir, generation=9)
        self.assertEqual(host, f"aios-9-{identity.node_name(self.dir)}")
        self.assertRegex(host, r"^aios-\d+-[a-z]+-[0-9a-f]{4}$")

    def test_the_generation_moves_and_the_name_does_not(self):
        """The point of the whole scheme: same machine, different incarnation."""
        eight = identity.hostname(self.dir, generation=8)
        nine = identity.hostname(self.dir, generation=9)
        self.assertNotEqual(eight, nine)
        self.assertEqual(eight.split("-", 2)[2], nine.split("-", 2)[2])

    def test_a_machine_that_never_booted_clean_is_generation_zero(self):
        with mock.patch.dict(os.environ, {"AIOS_ROOT": str(self.dir)}):
            self.assertTrue(identity.hostname(self.dir).startswith("aios-0-"))

    def test_the_hostname_is_a_legal_hostname(self):
        """It is passed to sethostname(2) and written into a shell prompt."""
        host = identity.hostname(self.dir, generation=123)
        self.assertLessEqual(len(host), 63, "a DNS label cannot exceed 63 octets")
        self.assertRegex(host, r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
        self.assertNotIn("_", host)


if __name__ == "__main__":
    unittest.main()
