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

**Verify:** run the suite in the target image — `docker run --rm -v "$PWD:/w:ro" -w /w <image> python3 -m unittest discover -s tests -t .`
