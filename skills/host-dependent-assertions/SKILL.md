---
name: host-dependent-assertions
description: Use when a test passes on one machine and fails on another, or when asserting that a real external command succeeds — installed binaries, compile flags, platform, network.
---

# Assert the relationship, not the outcome

**Failed:** `self.assertTrue(verdict.ok, verdict.detail)` on a verifier run against the real repo →

```
AssertionError: False is not true : forge probe failed:
vim: FAIL 4/5   FAIL no X11 or clipboard support (exit 1, expected 0)
```

Nothing was broken. An intent line forbids clipboard support; macOS vim ships `+clipboard`; the probe correctly caught it. The test asserted a property of **the laptop it was written on** — and on the target container it would fail differently, since vim isn't installed there at all.

**Why:** "run the real thing and assert success" silently depends on binaries, compile flags, platform, locale, network, clock. None of it is visible in the test source, so failures describe the host and get marked flaky.

**Fix:** assert that the wrapper *reports faithfully* — true on any host.

```python
verdict = agent.probe_verifier(repo, timeout_s=300)()
direct = subprocess.run([sys.executable, "-m", "forge", "probe"], cwd=repo,
                        capture_output=True, text=True, stdin=subprocess.DEVNULL)
self.assertEqual(verdict.ok, direct.returncode == 0)
```

When you genuinely need the host, skip loudly: `@unittest.skipUnless(shutil.which("vim"), "needs vim")`. A skip says "not covered"; a failure says "broken". Put the reason in the docstring, where the runner prints it.

## Seen again, two hours later

Same mistake, new test — a role probe asserting the machine had no ebuild tree and no
listening server, under a temporary root that could not affect either, because both
are absolute facts about the host:

```python
os.environ["AIOS_ROOT"] = tmp
r = node.role()
self.assertFalse(r.is_seed)          # true on a Mac, FALSE on the seed node
```

It passed locally and failed on the real node, where `is_seed` was correctly `True`.
Two things worth taking from the repeat: writing the skill does not stop the habit,
and the thing that actually caught it was a **release gate running the suite inside
the target** rather than any amount of local green. Put the gate where the code has
to run.

Fixed the same way as above: the measured call asserts only "returns without
raising", and the verdict is asserted against constructed values.

**Verify:** run the suite in the target image — `docker run --rm -v "$PWD:/w:ro" -w /w <image> python3 -m unittest discover -s tests -t .`
