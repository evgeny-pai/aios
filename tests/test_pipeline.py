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
from unittest import mock

from forge import lock as lock_mod
from forge import lower as lower_mod
from forge import minimize as minimize_mod
from forge import portage as portage_mod
from forge import probe as probe_mod
from forge import provider as provider_mod
from forge import spec as spec_mod
from forge.cli import main

#: The repo this suite is testing, found from the file rather than from the cwd —
#: the shipped spec and the shipped probes are part of what is under test.
REPO = Path(__file__).resolve().parents[1]

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

    def test_the_shipped_spec_owns_both_compiler_accelerators(self):
        """FEATURES is spec-owned, so not being in aios.toml means nothing sets it.

        A live lowering did propose `FEATURES="ccache buildpkg parallel-fetch"` and
        was overruled for it — the note is still in aios.lock.json — which is exactly
        why this line has to be written by a human, once.
        """
        features = spec_mod.load(REPO / "aios.toml").system.features.split()
        self.assertIn("buildpkg", features, "load-bearing for forge minimize")
        self.assertIn("ccache", features)
        self.assertIn("distcc", features)
        # ccache answers first: a cache hit should not cost a network round trip.
        self.assertLess(features.index("ccache"), features.index("distcc"))

    def test_the_shipped_spec_can_prove_the_distcc_intent(self):
        """An intent with no probe is lowerable but never minimizable."""
        self.assertIn("distcc", spec_mod.load(REPO / "aios.toml").all_probes())

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

    def test_the_prompt_names_every_key_that_will_be_refused(self):
        """A live lowering proposed PORTAGE_BINHOST and had it thrown away; the distcc
        intent makes DISTCC_HOSTS exactly the same trap. Saying so up front costs a
        line and saves a round trip, and the refusal still stands if it is ignored.
        """
        for key in (*lock_mod.FORBIDDEN, *lock_mod.SPEC_OWNED):
            with self.subTest(key=key):
                self.assertIn(key, lower_mod.SYSTEM_PROMPT)

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

    def test_shipped_distcc_probe_needs_no_remote_host(self):
        """Nothing on this mesh runs distccd, so a check requiring one is red forever.

        Verified the way skills/vacuous-probe-checks demands, on a macOS host where
        distcc is absent: `forge probe distcc` reports `distcc: FAIL 0/4`. All four,
        including the FEATURES one — that check names the binary as well as the
        setting, which is what stops it passing on any machine that merely has
        portage. Its other half is a *configuration* assertion and cannot be
        exercised without a portage, so it is asserted against a stand-in
        `emerge --info` in TestFeatureChecksAreWholeWord rather than claimed here.
        """
        distcc = probe_mod.load("distcc", REPO / "probes")
        self.assertEqual(distcc.atoms, ("sys-devel/distcc",))
        self.assertGreaterEqual(len(distcc.checks), 4)
        for check in distcc.checks:
            with self.subTest(check=check.name):
                # The subject has to set the exit status — no escape hatches.
                self.assertNotIn("|| true", check.script)
                self.assertNotIn("distccd", check.script, "no check may need a peer")
        # The one that used to be the FEATURES grep alone, which is a fact about
        # portage being installed and can never go red for a missing distcc. Naming
        # the binary too is what turns a misleading 1/4 back into 0/4.
        config = [c for c in distcc.checks if 'FEATURES="' in c.script]
        self.assertEqual(len(config), 1)
        self.assertIn("command -v distcc", config[0].script)


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

    def test_build_with_distcc_says_what_it_did_and_leaves_the_lock_alone(self):
        self.run_forge("init")
        self.run_forge("lower")
        # No network in this suite. A refused connection is also the honest state of
        # every machine that has no share daemon, which is most of them.
        with mock.patch("aios.mesh._urlopen", side_effect=OSError("no share daemon")):
            code = self.run_forge("build", "--distcc")
        self.assertEqual(code, 1, "there is no emerge on a dev host")
        self.assertIn("emerge is not on PATH", self.out)
        # The caveat travels with the flag: an empty host list is a local build, and
        # the note names which of the several possible reasons made it empty — here,
        # that the mesh itself did not answer.
        self.assertIn("No mesh peer offers a compile slot", self.err)
        self.assertIn("falls back", self.err)
        self.assertNotIn("DISTCC_HOSTS", self.lock.read_text(encoding="utf-8"))

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

    def spec_with_features(self, features: str) -> None:
        """A spec whose FEATURES this test chose. `forge init` cannot vary it."""
        self.spec.write_text(
            '[system]\narch = "aarch64"\nlibc = "musl"\n'
            f'features = "{features}"\n\n'
            '[agent]\nprovider = "echo"\n\n'
            '[[intent]]\ntext = "edit code over ssh, no X11"\nprobes = ["vim"]\n',
            encoding="utf-8",
        )

    def build_with_distcc(self, features: str) -> str:
        """`forge build --distcc` against a lock carrying `features`. Returns stderr."""
        self.spec_with_features(features)
        self.assertEqual(self.run_forge("lower"), 0)
        # No network in this suite, and a refused connection is also the honest state
        # of every machine with no share daemon — which is all of them today.
        with mock.patch("aios.mesh._urlopen", side_effect=OSError("no share daemon")):
            self.run_forge("build", "--distcc")
        return self.err

    def test_distcc_says_when_the_lock_would_ignore_the_host_list(self):
        """A quiet mesh and a lock without the feature look identical on the terminal.

        The host list is exported either way, so without this the operator debugs the
        network for what is a lockfile problem: portage never routes a compile through
        distcc unless FEATURES says so, and FEATURES is spec-owned.
        """
        err = self.build_with_distcc("buildpkg parallel-fetch")
        self.assertIn("FEATURES has no `distcc`", err)
        self.assertIn("forge lower", err, "say what fixes it")
        # Before the host list, not instead of it: the mesh note still follows. Asserted
        # by position rather than by quoting `aios.mesh`, whose wording is not this
        # test's subject (skills/negative-assertions — match what only this can produce).
        self.assertTrue(err.lstrip().startswith("the lockfile's FEATURES has no"), err)
        self.assertGreater(len(err.strip().splitlines()), 3, "the mesh note is missing")

    def test_distcc_is_quiet_when_the_lock_does_route_compiles_through_it(self):
        err = self.build_with_distcc("buildpkg distcc")
        self.assertNotIn("FEATURES has no", err)

    def test_a_feature_that_merely_starts_with_distcc_does_not_satisfy_it(self):
        """Whole word, not substring: `distcc-pump` is a different feature."""
        self.assertIn("FEATURES has no `distcc`", self.build_with_distcc("distcc-pump"))
        self.assertIn("FEATURES has no `distcc`", self.build_with_distcc("-distcc"))


class TestEmergeShapes(unittest.TestCase):
    """The invocation differs by job, and each difference costs or saves real time."""

    def setUp(self):
        self.lock = fresh_lock()

    def test_full_build_walks_the_graph(self):
        argv = portage_mod.emerge_argv(self.lock)
        self.assertIn("--deep", argv)
        self.assertNotIn("--oneshot", argv)
        self.assertIn("--changed-use", argv)

    def test_minimizer_shape_skips_the_graph_and_world(self):
        argv = portage_mod.emerge_argv(
            self.lock, atoms=["app-editors/vim"], deep=False, oneshot=True
        )
        self.assertNotIn("--deep", argv, "one atom per lever must not re-resolve the graph")
        self.assertIn("--oneshot", argv, "measuring must not rewrite @world dozens of times")
        self.assertIn("--changed-use", argv, "the flag flip is the one thing that moved")
        self.assertIn("app-editors/vim", argv)

    def test_binhost_rejects_mismatched_flags_rather_than_installing_them(self):
        argv = portage_mod.emerge_argv(self.lock, binhost=True)
        self.assertIn("--getbinpkg", argv)
        self.assertIn("--usepkg", argv)
        # Without this, a peer's vim built +X installs over an intent saying no X11.
        self.assertIn("--binpkg-respect-use=y", argv)

    def test_binhost_env_keeps_the_peer_out_of_the_lockfile(self):
        env = portage_mod.binhost_env("http://aios-repo:8080/binpkgs")
        self.assertEqual(env["PORTAGE_BINHOST"], "http://aios-repo:8080/binpkgs")
        # Which peers exist is a fact about this moment, not about the machine —
        # two nodes with the same spec must still render identical portage trees.
        rendered = portage_mod._make_conf(self.lock)
        self.assertNotIn("PORTAGE_BINHOST", rendered)

    def test_a_distcc_entry_is_host_slash_limit(self):
        self.assertEqual(portage_mod.distcc_host("mabruk4.local", 8), "mabruk4.local/8")
        # One malformed entry makes distcc reject the entire list, so a peer that
        # cannot count its cores must not cost this build every other peer.
        self.assertEqual(portage_mod.distcc_host("mabruk4.local", 0), "mabruk4.local/1")

    def test_distcc_hosts_keep_the_same_side_of_the_boundary_as_a_binhost(self):
        env = portage_mod.distcc_env(["mabruk4.local/8", "other.local/4"])
        self.assertEqual(env["DISTCC_HOSTS"], "mabruk4.local/8 other.local/4")
        rendered = portage_mod._make_conf(self.lock)
        self.assertNotIn("DISTCC_HOSTS", rendered)

    def test_an_empty_distcc_host_list_is_a_local_build_not_an_error(self):
        """The normal case here: nothing on the mesh accepts compile jobs yet."""
        env = portage_mod.distcc_env([])
        self.assertEqual(env["DISTCC_HOSTS"], "")
        # Exported empty on purpose. Unset, distcc would read /etc/distcc/hosts and
        # the build would depend on a file this pipeline never renders.
        self.assertEqual(env["DISTCC_FALLBACK"], "1")

    def test_features_are_spec_owned_but_the_host_list_is_never(self):
        """The two halves of a distributed compile, and only one is in the artifact."""
        spec = spec_mod.parse({
            "system": {"arch": "aarch64", "libc": "musl",
                       "features": "buildpkg ccache distcc"},
            "agent": {"provider": "echo"},
            "intent": [{"text": "edit code over ssh, no X11", "probes": ["vim"]}],
        })
        make_conf = portage_mod._make_conf(
            lower_mod.lower(spec, provider_mod.load("echo"))
        )
        # What this machine IS: it routes compiles through distcc.
        self.assertIn('FEATURES="buildpkg ccache distcc"', make_conf)
        # Who it can reach: not here, not ever.
        self.assertNotIn("DISTCC_HOSTS", make_conf)


class TestForbiddenKeys(unittest.TestCase):
    """Topology must not reach the lockfile, however plausibly it is proposed."""

    def test_binhost_is_refused_and_the_refusal_is_recorded(self):
        lowered = {
            "packages": [{"atom": "app-editors/vim", "why": "intent[0]", "use": [],
                          "accept_keywords": [], "probes": []}],
            # Exactly what a live claude-opus-5 lowering proposed. The hostname does
            # not resolve, and baking any peer in would make two nodes with the same
            # spec render different portage trees.
            "make_conf": [
                {"key": "PORTAGE_BINHOST", "value": "http://peer.aios.local/binpkgs/",
                 "why": "intent[4]: consume the peer's binhost"},
            ],
            "notes": [],
        }
        lock = lock_mod.build(SPEC, lowered, {"provider": "echo", "model": "t"})
        keys = {e["key"] for e in lock["make_conf"]}
        self.assertNotIn("PORTAGE_BINHOST", keys, "topology may not enter the lock")
        self.assertTrue(
            any("refused agent-proposed PORTAGE_BINHOST" in n for n in lock["notes"]),
            "a refusal must be recorded, not silent",
        )
        self.assertTrue(
            any("--peer" in n for n in lock["notes"]),
            "the refusal must name the supported route",
        )

    def test_distcc_hosts_are_refused_the_same_way(self):
        lowered = {
            "packages": [{"atom": "app-editors/vim", "why": "intent[0]", "use": [],
                          "accept_keywords": [], "probes": []}],
            # The plausible mistake: the intent asks to spread compiles across the
            # network, so a lowering pass writes down the machines that were up when
            # it ran. They are the wrong thing to freeze — FEATURES="distcc" belongs
            # in the lock (via the spec), the host list belongs to the moment.
            "make_conf": [
                {"key": "DISTCC_HOSTS", "value": "mabruk4.local/8 other.local/4",
                 "why": "intent[6]: distribute compilation"},
            ],
            "notes": [],
        }
        lock = lock_mod.build(SPEC, lowered, {"provider": "echo", "model": "t"})
        keys = {e["key"] for e in lock["make_conf"]}
        self.assertNotIn("DISTCC_HOSTS", keys, "topology may not enter the lock")
        self.assertTrue(
            any("refused agent-proposed DISTCC_HOSTS" in n for n in lock["notes"]),
            "a refusal must be recorded, not silent",
        )
        self.assertTrue(
            any("--distcc" in n for n in lock["notes"]),
            "the refusal must name the supported route",
        )

    def test_rendered_make_conf_never_carries_a_peer(self):
        lock = fresh_lock()
        for key in ("PORTAGE_BINHOST", "DISTCC_HOSTS"):
            self.assertNotIn(key, portage_mod._make_conf(lock))

    def test_a_rendered_tree_on_disk_carries_no_topology_either(self):
        """`_make_conf` is the unit; this is the file portage will actually read."""
        lock = fresh_lock()
        with tempfile.TemporaryDirectory() as tmp:
            portage_mod.render(lock, tmp)
            for path in (Path(tmp) / "etc" / "portage").rglob("*"):
                if path.is_file():
                    body = path.read_text(encoding="utf-8")
                    for key in ("PORTAGE_BINHOST", "DISTCC_HOSTS"):
                        self.assertNotIn(key, body, path)


def assignments(make_conf: str) -> dict[str, str]:
    """`KEY="VALUE"` lines as data, comments dropped.

    Parsed rather than grepped so an absence assertion cannot be satisfied by the
    comment that explains the ban (skills/negative-assertions): every test below
    asks whether DISTCC_HOSTS is a *variable*, not whether the word appears.
    """
    out: dict[str, str] = {}
    for line in make_conf.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key] = value.strip('"')
    return out


class TestOneEntryIsOneAssignment(unittest.TestCase):
    """`FORBIDDEN` is a check on the KEY, and make.conf is `KEY="VALUE"`.

    So a value carrying the quote that ends the assignment describes two variables,
    and the second one is whatever wrote it — topology the key check never saw, in a
    lockfile whose digest then certifies it. That defeats the guarantee the lockfile
    exists to make: two nodes with identical specs render identical portage trees.
    """

    #: The escape itself: close PKGDIR, open DISTCC_HOSTS on the next line.
    ESCAPE = 'x"\nDISTCC_HOSTS="evil.example.com/8'

    def lowered(self, key="PKGDIR", value="x", why="intent[0]: cache"):
        return {
            "packages": [{"atom": "app-editors/vim", "why": "intent[0]", "use": [],
                          "accept_keywords": [], "probes": []}],
            "make_conf": [{"key": key, "value": value, "why": why}],
            "notes": [],
        }

    def test_the_agent_boundary_refuses_a_value_that_closes_the_quote(self):
        with self.assertRaises(lower_mod.LoweringError) as caught:
            lower_mod.validate(self.lowered(value=self.ESCAPE), SPEC)
        self.assertIn("PKGDIR", str(caught.exception))

    def test_a_newline_alone_is_refused_too(self):
        """Half the escape is enough: a bare newline still starts a line portage obeys."""
        with self.assertRaises(lower_mod.LoweringError):
            lower_mod.validate(self.lowered(value="x\nDISTCC_HOSTS=evil/8"), SPEC)
        for value in ('x"y', "x\\", "x\ry"):
            with self.subTest(value=value), self.assertRaises(lower_mod.LoweringError):
                lower_mod.validate(self.lowered(value=value), SPEC)

    def test_a_key_that_is_not_a_variable_name_is_refused(self):
        for key in ('PKGDIR"\nDISTCC_HOSTS', "pkgdir", "PKG DIR", "1PKG", ""):
            with self.subTest(key=key), self.assertRaises(lower_mod.LoweringError):
                lower_mod.validate(self.lowered(key=key), SPEC)

    def test_a_newline_in_the_why_is_refused(self):
        """The `why` is interpolated too, and it walks out of its `#` the same way."""
        with self.assertRaises(lower_mod.LoweringError):
            lower_mod.validate(self.lowered(why="cache\nDISTCC_HOSTS=\"evil/8"), SPEC)

    def test_the_renderer_refuses_a_lock_that_already_carries_the_escape(self):
        """The second defence, and the only one a hand-edited lock still meets.

        `lock.build` refuses PORTAGE_BINHOST and DISTCC_HOSTS by key and stamps a
        digest over everything else, so a lock that arrived any other way than through
        `validate` has to be stopped here rather than become a /etc/portage that says
        more than the lockfile does.
        """
        lock = lock_mod.build(SPEC, self.lowered(value=self.ESCAPE),
                              {"provider": "echo", "model": "t"})
        lock_mod.verify(lock)  # the digest certifies it, which is exactly the problem
        self.assertEqual(
            [e["value"] for e in lock["make_conf"] if e["key"] == "PKGDIR"],
            [self.ESCAPE],
            "the fixture must really carry the escape into the lock",
        )
        with self.assertRaises(portage_mod.RenderError):
            portage_mod._make_conf(lock)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(portage_mod.RenderError):
                portage_mod.render(lock, tmp)
            self.assertFalse(
                (Path(tmp) / "etc" / "portage" / "make.conf").exists(),
                "a refused render must not leave half a portage tree behind",
            )

    def test_an_accepted_value_is_still_exactly_one_assignment(self):
        """The positive half: the same payload, minus the characters that break out.

        A fix that only pattern-matched `DISTCC_HOSTS` would pass the tests above and
        fail this one — the value here is allowed to *contain* those words, because it
        cannot stop being a value.
        """
        harmless = "x DISTCC_HOSTS=evil.example.com/8"
        lower_mod.validate(self.lowered(value=harmless), SPEC)
        lock = lock_mod.build(SPEC, self.lowered(value=harmless),
                              {"provider": "echo", "model": "t"})
        rendered = assignments(portage_mod._make_conf(lock))
        self.assertEqual(rendered["PKGDIR"], harmless)
        self.assertNotIn("DISTCC_HOSTS", rendered)


class TestDistccHostsAreNotShellCode(unittest.TestCase):
    """`distcc_host` renders into `export DISTCC_HOSTS="…"`, which a caller evals.

    The host reaching it is a name another machine chose for itself, in JSON. distcc's
    own grammar reads `@name` as "ssh there" and `name:COMMAND` as "and run this", and
    a `"` ends the export line — so an unvalidated peer name chooses where this
    machine's preprocessed source goes, or what runs. `aios.test_mesh` covers the
    quoting of the printed line; this covers the rule itself, where it lives.
    """

    def test_a_host_that_is_not_a_hostname_never_becomes_an_entry(self):
        for host in ('a";touch "$T/PWNED";x="', "@attacker.example.com",
                     "peer.local:sh -c 'curl x'", "peer.local other.local",
                     "peer.local|sh", "peer.local$(id)", ""):
            with self.subTest(host=host), self.assertRaises(ValueError):
                portage_mod.distcc_host(host, 8)

    def test_the_names_a_real_peer_has_still_work(self):
        self.assertEqual(portage_mod.distcc_host("mabruk4.local", 8), "mabruk4.local/8")
        self.assertEqual(portage_mod.distcc_host("10.0.0.9:3632", 4), "10.0.0.9:3632/4")

    def test_nothing_shell_active_survives_into_the_exported_value(self):
        """The property, asserted over the whole rendered value rather than per host."""
        hosts = [portage_mod.distcc_host(name, 8)
                 for name in ("mabruk4.local", "10.0.0.9", "10.0.0.9:3632")]
        value = portage_mod.distcc_env(hosts)["DISTCC_HOSTS"]
        self.assertRegex(value, r"^[A-Za-z0-9._:/ -]+$")
        self.assertEqual(value, "mabruk4.local/8 10.0.0.9/8 10.0.0.9:3632/8")


class TestFeatureChecksAreWholeWord(unittest.TestCase):
    """Both probes assert a spec-owned FEATURES, and `grep -q ccache` is not that.

    `FEATURES="-ccache"` is the setting that turns the feature OFF and a bare grep
    passes on it, as does an unrelated `ccache-something`. aios.toml puts ccache and
    distcc in FEATURES specifically so these checks pass, which makes the difference
    load-bearing rather than cosmetic: a check that cannot go red licenses the
    minimizer to drop what it claims to protect (skills/vacuous-probe-checks).

    The stand-in `emerge` is the point — asserting against the real one would encode
    a fact about this laptop (skills/host-dependent-assertions).
    """

    def config_check(self, probe: str) -> probe_mod.Check:
        """The check that asserts FEATURES, found by what it does, not by its index."""
        checks = [c for c in probe_mod.load(probe, REPO / "probes").checks
                  if 'FEATURES="' in c.script]
        self.assertEqual(len(checks), 1, f"{probe}: exactly one FEATURES check expected")
        return checks[0]

    def verdict(self, probe: str, features: str, *binaries: str) -> bool:
        """Run that one check against an `emerge --info` this test wrote."""
        check = self.config_check(probe)
        with tempfile.TemporaryDirectory() as bindir:
            for name, body in (("emerge", f"printf 'FEATURES=\"{features}\"\\n'"),
                               *((b, "exit 0") for b in binaries)):
                stub = Path(bindir) / name
                stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
                stub.chmod(0o755)
            one = probe_mod.Probe(name=probe, description="", atoms=(), checks=(check,))
            with mock.patch.dict(
                os.environ, {"PATH": f"{bindir}:{os.environ.get('PATH', '')}"}
            ):
                return probe_mod.run(one).passed

    def test_ccache_enabled_passes_and_disabled_fails(self):
        self.assertTrue(self.verdict("ccache", "buildpkg ccache"))
        self.assertTrue(self.verdict("ccache", "ccache"), "the only feature, no spaces")
        self.assertFalse(self.verdict("ccache", "buildpkg -ccache"),
                         "-ccache is the setting that turns it OFF")
        self.assertFalse(self.verdict("ccache", "buildpkg ccache-something"))
        self.assertFalse(self.verdict("ccache", "buildpkg parallel-fetch"))

    def test_distcc_enabled_passes_and_disabled_fails(self):
        """Same rule, and the distcc check also has to find the binary to pass."""
        self.assertTrue(self.verdict("distcc", "buildpkg distcc", "distcc"))
        self.assertFalse(self.verdict("distcc", "buildpkg -distcc", "distcc"))
        self.assertFalse(self.verdict("distcc", "buildpkg distcc-pump", "distcc"))
        # The half that made this check vacuous: FEATURES is rendered from the spec
        # whether or not sys-devel/distcc exists, so without the binary it must fail.
        self.assertFalse(self.verdict("distcc", "buildpkg distcc"))

    def test_the_shipped_spec_still_says_what_these_checks_assert(self):
        """These are configuration assertions, so aios.toml is half of each one."""
        features = spec_mod.load(REPO / "aios.toml").system.features.split()
        for feature in ("ccache", "distcc"):
            self.assertIn(feature, features)


if __name__ == "__main__":
    unittest.main()
