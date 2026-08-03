"""The reaper that makes PID 1 an init.

One of these reaps a real process; the rest use injected seams, because the interesting
behaviours are "does not exit on a surprise" and "does not spin" — both of which a real
blocking waitpid would make slow or untestable.
"""

from __future__ import annotations

import os
import signal
import unittest
from unittest import mock

from aios import reaper as reaper_mod


class TestReapForever(unittest.TestCase):
    def test_it_reaps_until_told_to_stop(self):
        exits = [(101, 0), (102, 0), (103, 0)]
        calls = {"n": 0}

        def waitpid(_pid, _flags):
            calls["n"] += 1
            if exits:
                return exits.pop(0)
            raise ChildProcessError

        reaped = reaper_mod.reap_forever(
            waitpid=waitpid,
            sleep=lambda _s: None,
            should_stop=lambda: calls["n"] >= 4,
        )
        self.assertEqual(reaped, 3)

    def test_no_children_sleeps_instead_of_spinning(self):
        """The idle case is most of a node's uptime; a hot loop there is a busy core."""
        slept = []

        def waitpid(_pid, _flags):
            raise ChildProcessError

        reaper_mod.reap_forever(
            waitpid=waitpid,
            sleep=slept.append,
            should_stop=lambda: len(slept) >= 3,
            idle_sleep_s=0.25,
        )
        self.assertEqual(slept, [0.25, 0.25, 0.25])

    def test_an_interrupted_wait_is_retried_not_fatal(self):
        seq = [InterruptedError(), (7, 0)]

        def waitpid(_pid, _flags):
            item = seq.pop(0) if seq else ChildProcessError()
            if isinstance(item, BaseException):
                raise item
            return item

        reaped = reaper_mod.reap_forever(
            waitpid=waitpid, sleep=lambda _s: None, should_stop=lambda: not seq
        )
        self.assertEqual(reaped, 1, "the child after the interruption still got reaped")

    def test_an_unexpected_oserror_does_not_kill_the_init(self):
        """A PID 1 that returns is a CrashLoopBackOff; it must outlive a surprise."""
        raised = {"n": 0}

        def waitpid(_pid, _flags):
            raised["n"] += 1
            raise OSError(22, "invalid argument")

        # Returns normally rather than propagating.
        reaper_mod.reap_forever(
            waitpid=waitpid, sleep=lambda _s: None, should_stop=lambda: raised["n"] >= 2
        )
        self.assertGreaterEqual(raised["n"], 2)

    def test_it_reaps_a_real_child(self):
        """The actual syscall, on a process that really exited.

        Not a zombie reparented from elsewhere — that requires being PID 1, which a test
        is not. A direct child is the same waitpid path and is what the loop does all day.
        """
        pid = os.fork()
        if pid == 0:  # child
            os._exit(0)
        done = {"seen": False}

        def stop():
            if done["seen"]:
                return True
            done["seen"] = True
            return False

        reaped = reaper_mod.reap_forever(sleep=lambda _s: None, should_stop=stop)
        self.assertEqual(reaped, 1)
        # And it is really gone from the table: waiting again finds no such child.
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, 0)


class TestSignals(unittest.TestCase):
    def test_term_and_int_are_handled_so_the_pod_can_be_deleted(self):
        """PID 1 is exempt from every signal's DEFAULT action, so unhandled TERM is
        ignored outright and `kubectl delete pod` has to fall back to SIGKILL after the
        grace period. Installing handlers is what makes shutdown work."""
        before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            installed = reaper_mod.install_handlers()
            self.assertIn(signal.SIGTERM, installed)
            self.assertIn(signal.SIGINT, installed)
            self.assertIs(signal.getsignal(signal.SIGTERM), reaper_mod._raise_terminated)
        finally:
            for sig, handler in before.items():
                signal.signal(sig, handler)

    def test_the_handler_raises_so_a_blocking_waitpid_unwinds(self):
        """A flag would never be read: PEP 475 retries the syscall after the handler
        returns, so a blocking wait on a child that never exits would sit there forever."""
        with self.assertRaises(reaper_mod.Terminated) as caught:
            reaper_mod._raise_terminated(signal.SIGTERM, None)
        self.assertEqual(caught.exception.args[0], signal.SIGTERM)

    def test_main_stops_cleanly_on_termination(self):
        with mock.patch.object(reaper_mod, "install_handlers", return_value=[]), \
             mock.patch.object(
                 reaper_mod, "reap_forever", side_effect=reaper_mod.Terminated(signal.SIGTERM)
             ):
            self.assertEqual(reaper_mod.main([]), 0)


if __name__ == "__main__":
    unittest.main()
