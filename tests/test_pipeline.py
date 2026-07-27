"""Offline tests: no network, no portage, no credentials.

    python3 -m unittest discover -s tests -t .

Everything here runs against the echo backend and the dry runner, which is the
point of both — the pipeline below the lowering pass has to be testable without
a model, and the search loop has to be testable without a Gentoo host.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from forge import lock as lock_mod
from forge import lower as lower_mod
from forge import minimize as minimize_mod
from forge import portage as portage_mod
from forge import probe as probe_mod
from forge import provider as provider_mod
from forge import spec as spec_mod
from forge.cli import main

SPEC = spec_mod.parse(
    {
        "system": {"name": "t", "arch": "aarch64", "libc": "musl", "cflags": "-O2"},
        "agent": {"provider": "echo"},
        "intent": [
            {"text": "edit code over ssh, no X11", "probes": ["vim"]},
            {"text": "build C projects"},
        ],
    }
)


def fresh_lock() -> dict:
    return lower_mod.lower(SPEC, provider_mod.load("echo"))


class TestSpec(unittest.TestCase):
    def test_chost_follows_arch_and_libc(self):
        self.assertEqual(SPEC.system.chost, "aarch64-unknown-linux-musl")
        glibc = spec_mod.System(arch="x86_64", libc="glibc")
        self.assertEqual(glibc.chost, "x86_64-unknown-linux-gnu")

    def test_keyword_is_portage_arch_not_uname(self):
        self.assertEqual(SPEC.system.keyword, "arm64")
        self.assertEqual(spec_mod.System(arch="x86_64").keyword, "amd64")

    def test_rejects_unknown_arch(self):
        with self.assertRaises(spec_mod.SpecError):
            spec_mod.parse({"system": {"arch": "sparc"}, "intent": [{"text": "x"}]})

    def test_requires_intent(self):
        with self.assertRaises(spec_mod.SpecError):
            spec_mod.parse({"system": {}})

    def test_starter_spec_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "aios.toml"
            path.write_text(spec_mod.STARTER, encoding="utf-8")
            spec = spec_mod.load(path)
        self.assertEqual(spec.all_probes(), ("vim",))


class TestLock(unittest.TestCase):
    def test_canonical_is_stable(self):
        a = fresh_lock()
        b = fresh_lock()
        self.assertEqual(lock_mod.canonical(a), lock_mod.canonical(b))
        self.assertEqual(a["digest"], b["digest"])

    def test_digest_detects_hand_edits(self):
        lock = fresh_lock()
        lock_mod.verify(lock)
        lock["packages"][0]["use"][0]["enabled"] = not lock["packages"][0]["use"][0]["enabled"]
        with self.assertRaises(lock_mod.LockError):
            lock_mod.verify(lock)

    def test_spec_owns_toolchain_flags(self):
        lowered = {
            "packages": [
                {"atom": "app-editors/vim", "why": "intent[0]", "use": [],
                 "accept_keywords": [], "probes": []}
            ],
            "make_conf": [
                {"key": "CFLAGS", "value": "-O3 -march=native", "why": "faster"},
                # FEATURES became spec-owned after a live lowering replaced
                # buildpkg with parallel-fetch. buildpkg is not a preference: it is
                # what makes forge minimize affordable and what a node serves to its
                # peers, so a model must not be able to drop it.
                {"key": "FEATURES", "value": "parallel-fetch", "why": "faster fetches"},
                {"key": "EMERGE_DEFAULT_OPTS", "value": "--jobs=2", "why": "intent[1]"},
            ],
            "notes": [],
        }
        lock = lock_mod.build(SPEC, lowered, {"provider": "echo", "model": "t"})
        entries = {e["key"]: e["value"] for e in lock["make_conf"]}
        self.assertEqual(entries["CFLAGS"], "-O2", "spec must win over the agent")
        self.assertIn("buildpkg", entries["FEATURES"], "the agent may not drop buildpkg")
        self.assertEqual(entries["EMERGE_DEFAULT_OPTS"], "--jobs=2", "non-owned keys survive")
        for key in ("CFLAGS", "FEATURES"):
            self.assertTrue(
                any(f"discarded agent-proposed {key}" in n for n in lock["notes"]),
                f"{key} override must be recorded, not silently dropped",
            )

    def test_drops_probe_references_no_intent_names(self):
        lowered = {
            "packages": [
                {"atom": "app-editors/vim", "why": "intent[0]", "use": [],
                 "accept_keywords": [], "probes": ["vim", "emacs"]}
            ],
            "make_conf": [],
            "notes": [],
        }
        lock = lock_mod.build(SPEC, lowered, {"provider": "echo", "model": "t"})
        self.assertEqual(lock_mod.package(lock, "app-editors/vim")["probes"], ["vim"])
        self.assertTrue(any("emacs" in note for note in lock["notes"]))

    def test_diff_reports_flag_flips(self):
        old = fresh_lock()
        new = json.loads(json.dumps(old))
        flag = lock_mod.package(new, "app-editors/vim")["use"][0]
        flag["enabled"] = not flag["enabled"]
        flag["why"] = "changed"
        lines = lock_mod.diff(old, new)
        self.assertTrue(any("app-editors/vim" in line and "->" in line for line in lines))
        self.assertEqual(lock_mod.diff(old, old), [])


class TestLowering(unittest.TestCase):
    def test_echo_roundtrips_into_a_lock(self):
        lock = fresh_lock()
        atoms = [p["atom"] for p in lock["packages"]]
        self.assertIn("app-editors/vim", atoms)
        self.assertEqual(atoms, sorted(atoms), "packages must be canonically ordered")

    def test_every_flag_carries_provenance(self):
        lock = fresh_lock()
        for pkg in lock["packages"]:
            self.assertTrue(pkg["why"].strip(), pkg["atom"])
            for flag in pkg["use"]:
                self.assertTrue(flag["why"].strip(), f"{pkg['atom']}:{flag['flag']}")

    def test_rejects_a_flag_with_no_why(self):
        payload = {
            "packages": [
                {"atom": "app-editors/vim", "why": "intent[0]",
                 "use": [{"flag": "syntax", "enabled": True, "why": "  "}],
                 "accept_keywords": [], "probes": []}
            ],
            "make_conf": [], "notes": [],
        }
        with self.assertRaises(lower_mod.LoweringError):
            lower_mod.validate(payload, SPEC)

    def test_rejects_a_bare_package_name(self):
        payload = {
            "packages": [{"atom": "vim", "why": "intent[0]", "use": [],
                          "accept_keywords": [], "probes": []}],
            "make_conf": [], "notes": [],
        }
        with self.assertRaises(lower_mod.LoweringError):
            lower_mod.validate(payload, SPEC)

    def test_rejects_an_empty_lowering(self):
        with self.assertRaises(lower_mod.LoweringError):
            lower_mod.validate({"packages": [], "make_conf": [], "notes": []}, SPEC)

    def test_schema_is_portable_across_backends(self):
        """Closed objects, all properties required, no unsupported keywords."""
        banned = {"minimum", "maximum", "minLength", "maxLength", "pattern", "multipleOf"}

        def walk(node, path="$"):
            if not isinstance(node, dict):
                return
            if node.get("type") == "object":
                self.assertIs(node.get("additionalProperties"), False, path)
                props = set(node.get("properties", {}))
                self.assertEqual(set(node.get("required", [])), props, path)
            self.assertFalse(banned & set(node), f"{path}: unsupported schema keywords")
            for key, sub in node.get("properties", {}).items():
                walk(sub, f"{path}.{key}")
            if "items" in node:
                walk(node["items"], f"{path}[]")

        walk(lower_mod.SCHEMA)

    def test_prompt_carries_intents_and_target(self):
        prompt = lower_mod.build_prompt(SPEC)
        self.assertIn("intent[0]", prompt)
        self.assertIn("aarch64-unknown-linux-musl", prompt)
        self.assertIn("probes: vim", prompt)


class TestAnthropicPayload(unittest.TestCase):
    """`effort` is a Claude 5 knob; older models 400 on it."""

    def test_effort_is_gated_by_model_family(self):
        from forge.provider import anthropic

        for model in ("claude-opus-5", "claude-sonnet-5", "claude-fable-5"):
            self.assertTrue(anthropic.supports_effort(model), model)
        # Real failure this guards: claude-haiku-4-5 returns
        # HTTP 400 "This model does not support the effort parameter."
        for model in ("claude-haiku-4-5-20251001", "claude-3-5-sonnet-20241022"):
            self.assertFalse(anthropic.supports_effort(model), model)

    def test_unknown_models_omit_effort_rather_than_risk_a_400(self):
        from forge.provider import anthropic

        self.assertFalse(anthropic.supports_effort("some-future-model"))


class TestPortage(unittest.TestCase):
    def test_render_is_deterministic_and_annotated(self):
        lock = fresh_lock()
        with tempfile.TemporaryDirectory() as tmp:
            first = {p: p.read_text() for p in portage_mod.render(lock, Path(tmp) / "a")}
            second = {
                Path(str(p).replace("/b/", "/a/")): p.read_text()
                for p in portage_mod.render(lock, Path(tmp) / "b")
            }
            self.assertEqual(first, second)

            use = next(t for p, t in first.items() if p.name == "aios" and "package.use" in str(p))
            self.assertIn("app-editors/vim", use)
            self.assertIn("-X", use)
            self.assertIn("intent[0]: no X11", use)

            make_conf = next(t for p, t in first.items() if p.name == "make.conf")
            self.assertIn('CHOST="aarch64-unknown-linux-musl"', make_conf)
            self.assertIn('ACCEPT_KEYWORDS="arm64"', make_conf)
            self.assertNotIn(
                "PORTDIR_OVERLAY=", make_conf, "deprecated; repos.conf owns the overlay"
            )
            self.assertIn(lock["digest"], make_conf)

    def test_emerge_argv_forces_use_changes_to_apply(self):
        lock = fresh_lock()
        argv = portage_mod.emerge_argv(lock, root="/mnt/slot-b")
        self.assertIn("--newuse", argv)
        self.assertIn("--root=/mnt/slot-b", argv)
        self.assertIn("@aios", argv)
        self.assertIn("--pretend", portage_mod.emerge_argv(lock, pretend=True))

    def test_binhost_is_opt_in_and_fetches_remote_packages(self):
        lock = fresh_lock()
        self.assertNotIn("--getbinpkg", portage_mod.emerge_argv(lock))
        argv = portage_mod.emerge_argv(lock, binhost=True)
        # --usepkg alone would only accept local binpkgs; the pair is what makes
        # a peer's cache reachable.
        self.assertIn("--getbinpkg", argv)
        self.assertIn("--usepkg", argv)

    def test_binhost_never_reaches_the_lockfile(self):
        # A binhost is a fact about the network right now, not about the machine
        # being built: two nodes with the same spec must still render the same
        # portage tree. Guard the boundary that guarantees it.
        lock = fresh_lock()
        env = portage_mod.binhost_env("http://mac2:4748/api/build/binpkg/", "tok")
        rendered = portage_mod.render(lock, self.root_for_render())
        make_conf = (self.root_for_render() / "etc" / "portage" / "make.conf").read_text()
        self.assertNotIn("PORTAGE_BINHOST", make_conf)
        self.assertIn("PORTAGE_BINHOST", env)
        self.assertTrue(rendered)

    def test_binhost_env_carries_credentials_portage_can_use(self):
        env = portage_mod.binhost_env("http://mac2:4748/api/build/binpkg/", "tok3n")
        # wget/curl read credentials from the URL; there is no header to set.
        self.assertEqual(
            env["PORTAGE_BINHOST"], "http://portage:tok3n@mac2:4748/api/build/binpkg/"
        )
        self.assertIn("-binpkg-request-signature", env["FEATURES"])
        plain = portage_mod.binhost_env("http://mac2:4748/api/build/binpkg/")
        self.assertEqual(plain["PORTAGE_BINHOST"], "http://mac2:4748/api/build/binpkg/")

    def root_for_render(self) -> Path:
        if not hasattr(self, "_render_root"):
            self._render_tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self._render_tmp.cleanup)
            self._render_root = Path(self._render_tmp.name)
        return self._render_root


class TestProbes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, body):
        (self.dir / f"{name}.toml").write_text(body, encoding="utf-8")

    def test_passing_and_failing_checks(self):
        self.write(
            "ok",
            'name = "ok"\n'
            '[[check]]\nname = "writes into $T"\n'
            'script = """\nprintf hi > "$T/f"\ngrep -q hi "$T/f"\n"""\n',
        )
        self.write(
            "bad",
            'name = "bad"\n[[check]]\nname = "fails"\nscript = """\nexit 3\n"""\n',
        )
        ok = probe_mod.run(probe_mod.load("ok", self.dir))
        self.assertTrue(ok.passed, ok.checks[0].reason)

        bad = probe_mod.run(probe_mod.load("bad", self.dir))
        self.assertFalse(bad.passed)
        self.assertIn("exit 3", bad.failures[0].reason)

    def test_stdout_expectation(self):
        self.write(
            "out",
            'name = "out"\n[[check]]\nname = "greets"\n'
            'script = """\necho hello world\n"""\nexpect_stdout_contains = "hello"\n',
        )
        self.assertTrue(probe_mod.run(probe_mod.load("out", self.dir)).passed)

    def test_dry_run_skips_without_failing(self):
        self.write("x", 'name = "x"\n[[check]]\nname = "n"\nscript = """\nexit 1\n"""\n')
        result = probe_mod.run(probe_mod.load("x", self.dir), dry_run=True)
        self.assertTrue(result.passed)
        self.assertTrue(result.checks[0].skipped)

    def test_missing_probe_is_an_error(self):
        with self.assertRaises(probe_mod.ProbeError):
            probe_mod.load("nope", self.dir)

    def test_shipped_vim_probe_is_wellformed(self):
        vim = probe_mod.load("vim", "probes")
        self.assertEqual(vim.atoms, ("app-editors/vim",))
        self.assertGreaterEqual(len(vim.checks), 4)


class TestMinimize(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Path(self.tmp.name) / "journal.jsonl"
        self.addCleanup(self.tmp.cleanup)

    def test_loop_drops_levers_and_journals_every_attempt(self):
        lock = fresh_lock()
        levers = ["nls", "perl", "python"]
        result = minimize_mod.minimize(
            lock, "app-editors/vim", minimize_mod.DryRunner(),
            journal=self.journal, levers=levers,
        )
        self.assertTrue(result.simulated)
        self.assertEqual(result.dropped, sorted(levers))
        self.assertLess(result.final_size, result.baseline_size)

        records = [json.loads(line) for line in self.journal.read_text().splitlines()]
        self.assertEqual(records[0]["lever"], "baseline")
        self.assertEqual([r["lever"] for r in records[1:]], sorted(levers))
        self.assertTrue(all(r["verdict"] == "accepted" for r in records))

    def test_a_lever_that_breaks_probes_is_reverted(self):
        lock = fresh_lock()

        class BreaksOnSyntax(minimize_mod.DryRunner):
            def build(self, atom, disabled):
                self.broken = "syntax" in disabled
                return super().build(atom, disabled)

            def installed_size(self, atom):
                if getattr(self, "broken", False):
                    raise AssertionError("should not measure a build that fails its probes")
                return super().installed_size(atom)

        runner = BreaksOnSyntax()
        original = probe_mod.run_all

        def fake_run_all(names, **kwargs):
            if getattr(runner, "broken", False):
                return [
                    probe_mod.ProbeResult(
                        "vim", [probe_mod.CheckResult("syntax engine", False, "missing +syntax")]
                    )
                ]
            return [probe_mod.ProbeResult("vim", [probe_mod.CheckResult("syntax engine", True)])]

        minimize_mod.probe_mod.run_all = fake_run_all
        try:
            result = minimize_mod.minimize(
                lock, "app-editors/vim", runner,
                journal=self.journal, levers=["nls", "syntax"],
            )
        finally:
            minimize_mod.probe_mod.run_all = original

        self.assertEqual(result.dropped, ["nls"])
        verdicts = {a.lever: a.verdict for a in result.attempts}
        self.assertEqual(verdicts["syntax"], "rejected-probe")

    def test_refuses_a_package_with_no_probes(self):
        lock = fresh_lock()

        class Real(minimize_mod.DryRunner):
            simulated = False

        runner = Real()
        runner.simulated = False
        with self.assertRaises(minimize_mod.MinimizeError):
            minimize_mod.minimize(lock, "sys-devel/make", runner, journal=self.journal)

    def test_apply_writes_provenance_back_into_the_lock(self):
        lock = fresh_lock()
        result = minimize_mod.minimize(
            lock, "app-editors/vim", minimize_mod.DryRunner(),
            journal=self.journal, levers=["nls"],
        )
        updated = minimize_mod.apply(lock, result)
        flag = next(f for f in lock_mod.package(updated, "app-editors/vim")["use"] if f["flag"] == "nls")
        self.assertFalse(flag["enabled"])
        self.assertIn("minimize:", flag["why"])
        self.assertEqual(updated["minimized"]["app-editors/vim"]["dropped"], ["nls"])
        lock_mod.verify(updated)


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.spec = self.dir / "aios.toml"
        self.lock = self.dir / "aios.lock.json"
        os.environ["AIOS_PROVIDER"] = "echo"
        self.addCleanup(os.environ.pop, "AIOS_PROVIDER", None)

    def run_forge(self, *argv):
        """Run a command with its output captured, so the suite stays readable."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--spec", str(self.spec), "--lock", str(self.lock), *argv])
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def test_init_lower_render_cycle(self):
        self.assertEqual(self.run_forge("init"), 0)
        self.assertEqual(self.run_forge("lower"), 0)
        self.assertEqual(self.run_forge("diff"), 0)
        self.assertEqual(self.run_forge("show"), 0)
        self.assertIn("app-editors/vim", self.out)
        self.assertIn("NOT minimizable", self.out, "packages without probes must say so")
        self.assertEqual(self.run_forge("render", "--root", str(self.dir / "out")), 0)
        self.assertTrue((self.dir / "out" / "etc" / "portage" / "make.conf").is_file())

    def test_init_refuses_to_clobber(self):
        self.assertEqual(self.run_forge("init"), 0)
        self.assertEqual(self.run_forge("init"), 1)
        self.assertEqual(self.run_forge("init", "--force"), 0)

    def test_diff_flags_a_stale_lock(self):
        self.run_forge("init")
        self.run_forge("lower")
        self.spec.write_text(
            self.spec.read_text() + '\n[[intent]]\ntext = "serve static files over http"\n',
            encoding="utf-8",
        )
        self.assertEqual(self.run_forge("diff"), 1)

    def test_reseal_after_a_deliberate_edit(self):
        self.run_forge("init")
        self.run_forge("lower")
        raw = json.loads(self.lock.read_text())
        raw["notes"].append("hand edited")
        self.lock.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        with self.assertRaises(lock_mod.LockError):
            lock_mod.load(self.lock)
        self.assertEqual(self.run_forge("reseal"), 0)
        lock_mod.load(self.lock)


if __name__ == "__main__":
    unittest.main()
