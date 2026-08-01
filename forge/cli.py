"""forge — the AIos build tool."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import binpkg as binpkg_mod
from . import lock as lock_mod
from . import lower as lower_mod
from . import minimize as minimize_mod
from . import portage as portage_mod
from . import probe as probe_mod
from . import provider as provider_mod
from . import spec as spec_mod

SPEC_PATH = "aios.toml"


class Fail(Exception):
    """Anything the user needs to fix. Printed without a traceback."""


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 2
    try:
        return args.handler(args) or 0
    except (Fail, spec_mod.SpecError, lock_mod.LockError, probe_mod.ProbeError,
            provider_mod.ProviderError, lower_mod.LoweringError,
            minimize_mod.MinimizeError, binpkg_mod.BinpkgError,
            portage_mod.RenderError) as exc:
        print(f"forge: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nforge: interrupted", file=sys.stderr)
        return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge", description="Compile intent into a Linux system."
    )
    parser.add_argument("--spec", default=SPEC_PATH, help=f"spec path (default {SPEC_PATH})")
    parser.add_argument(
        "--lock", default=lock_mod.DEFAULT_PATH,
        help=f"lockfile path (default {lock_mod.DEFAULT_PATH})",
    )
    parser.add_argument("--probes", default=probe_mod.DEFAULT_DIR, help="probe directory")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="write a starter aios.toml")
    p.add_argument("--force", action="store_true", help="overwrite an existing spec")
    p.set_defaults(handler=cmd_init)

    p = sub.add_parser("lower", help="intent -> lockfile (calls a model)")
    p.add_argument("--provider", default="", help="override agent.provider")
    p.add_argument("--model", default="", help="override agent.model")
    p.add_argument("--dry-run", action="store_true", help="print the diff, do not write")
    p.set_defaults(handler=cmd_lower)

    p = sub.add_parser("show", help="what the lockfile decided, and why")
    p.add_argument("atom", nargs="?", help="limit to one package")
    p.set_defaults(handler=cmd_show)

    p = sub.add_parser("diff", help="check the lock against the spec and its digest")
    p.set_defaults(handler=cmd_diff)

    p = sub.add_parser("reseal", help="recompute the digest after a deliberate hand edit")
    p.set_defaults(handler=cmd_reseal)

    p = sub.add_parser("render", help="lockfile -> /etc/portage tree")
    p.add_argument("--root", default="./out", help="write under this root (default ./out)")
    p.add_argument("--overlay", default="", help="also write repos.conf for this overlay path")
    p.set_defaults(handler=cmd_render)

    p = sub.add_parser("build", help="emerge the lockfile's package set")
    p.add_argument("--root", default="/", help="build into this root")
    # One flag per mode, mutually exclusive, even though pretend is still the default.
    # `--pretend` was previously only expressible as "no --execute", which left
    # `--detach` no way to say what it contradicts.
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="really build (default is --pretend)")
    mode.add_argument("--pretend", action="store_true", help="print emerge's plan and build nothing — the default")
    p.add_argument("--detach", action="store_true", help="start the build as a job with NO time limit and print its id, then return. Survives this shell, your terminal and a reboot of the tmux client; poll it with `python3 -m aios.build status <id>`. Requires --execute")
    p.add_argument("--peer", default="", help="reuse a peer's binary packages, e.g. http://aios-repo:8080/binpkgs (env: AIOS_BINHOST). Prebuilt packages whose USE flags disagree with the lock are rejected, not installed")
    p.add_argument("--distcc", action="store_true", help="take the mesh's compile hosts as DISTCC_HOSTS. Needs FEATURES=distcc in the spec and a distccd on each peer; with neither, every compile runs locally and the build still works")
    p.set_defaults(handler=cmd_build)

    p = sub.add_parser(
        "binpkg",
        help="does a prebuilt package fit this lockfile?",
        description="Compare built binary packages against the lockfile's decisions. "
                    "Reads metadata only — never the image. Exits non-zero unless every "
                    "package is reusable here.",
    )
    p.add_argument("paths", nargs="*", help="package files (.gpkg.tar, .tbz2)")
    p.add_argument("--dir", default="", help=f"walk a binpkg cache (e.g. {binpkg_mod.DEFAULT_PKGDIR})")
    p.add_argument("--peer", default="", help="a peer's binhost URL, or one package URL over HTTP")
    p.add_argument("--atom", default="", help="compare against this lock entry instead of the package's own")
    p.set_defaults(handler=cmd_binpkg)

    p = sub.add_parser("probe", help="run capability checks")
    p.add_argument("names", nargs="*", help="probe names (default: every probe in the spec)")
    p.add_argument("--root", default="/", help="run inside this root via chroot")
    p.add_argument("--dry-run", action="store_true", help="list checks without running them")
    p.add_argument("-v", "--verbose", action="store_true", help="show output of failed checks")
    p.set_defaults(handler=cmd_probe)

    p = sub.add_parser("minimize", help="drop every feature the probes do not need")
    p.add_argument("atom", help="package to minimize, e.g. app-editors/vim")
    p.add_argument("--root", default="/", help="build and probe in this root")
    p.add_argument("--dry-run", action="store_true", help="simulate: exercise the loop, build nothing")
    p.add_argument("--lever", action="append", default=[], help="restrict to these levers")
    p.add_argument("--apply", action="store_true", help="write the result back into the lockfile")
    p.set_defaults(handler=cmd_minimize)

    return parser


# --- commands ---------------------------------------------------------------


def cmd_init(args) -> int:
    path = Path(args.spec)
    if path.exists() and not args.force:
        raise Fail(f"{path} already exists (--force to overwrite)")
    path.write_text(spec_mod.STARTER, encoding="utf-8")
    print(f"wrote {path}")
    print("Edit the intents, then: forge lower")
    return 0


def cmd_lower(args) -> int:
    spec = spec_mod.load(args.spec)
    _warn_missing_probes(spec, args.probes)

    provider = provider_mod.load(
        args.provider or spec.agent.provider,
        args.model or spec.agent.model,
        effort=spec.agent.effort,
    )
    print(f"lowering {len(spec.intents)} intents via {provider.describe()}", file=sys.stderr)

    new = lower_mod.lower(spec, provider)
    old = None
    if Path(args.lock).exists():
        try:
            old = lock_mod.load(args.lock)
        except lock_mod.LockError as exc:
            print(f"forge: ignoring existing lock ({exc.args[0].splitlines()[0]})", file=sys.stderr)

    changes = lock_mod.diff(old, new)
    for line in changes:
        print(line)
    if not changes:
        print("no change")
    for note in new["notes"]:
        print(f"note: {note}", file=sys.stderr)

    if args.dry_run:
        print("(--dry-run: nothing written)", file=sys.stderr)
        return 0
    path = lock_mod.save(new, args.lock)
    print(f"wrote {path}  {new['digest']}", file=sys.stderr)
    return 0


def cmd_show(args) -> int:
    lock = lock_mod.load(args.lock)
    system = lock["system"]
    if not args.atom:
        print(f"{system['name']}: {system['arch']}/{system['libc']}, init={system['init']}")
        print(f"lowered by {lock['generated_by']['provider']}:{lock['generated_by']['model']}")
        print(f"digest {lock['digest']}")
        print()
        print("make.conf")
        for entry in lock["make_conf"]:
            print(f"  {entry['key']}={entry['value']!r}")
            print(f"      {entry['why']}")
        print()

    for pkg in lock["packages"]:
        if args.atom and pkg["atom"] != args.atom:
            continue
        probes = ", ".join(pkg["probes"]) or "no probes — NOT minimizable"
        print(f"{pkg['atom']}")
        print(f"    why:    {pkg['why']}")
        print(f"    probes: {probes}")
        for flag in pkg["use"]:
            token = f"{'+' if flag['enabled'] else '-'}{flag['flag']}"
            print(f"    {token:<16} {flag['why']}")
        record = lock.get("minimized", {}).get(pkg["atom"])
        if record:
            print(
                f"    minimized: dropped {', '.join(record['dropped']) or 'nothing'} "
                f"({record['size_before']} -> {record['size_after']} bytes"
                f"{', simulated' if record.get('simulated') else ''})"
            )
        print()

    for note in lock["notes"]:
        print(f"note: {note}")
    return 0


def cmd_diff(args) -> int:
    spec = spec_mod.load(args.spec)
    lock = lock_mod.load(args.lock)
    stale = lock["spec_digest"] != lock_mod.spec_digest(spec)
    if stale:
        print("stale: the spec has changed since this lock was generated")
        print("       run `forge lower` and review the diff")
        return 1
    print(f"in sync: {args.spec} -> {args.lock}  {lock['digest']}")
    return 0


def cmd_reseal(args) -> int:
    lock = lock_mod.load(args.lock, check=False)
    before = lock.get("digest")
    lock_mod.save(lock, args.lock)
    print(f"resealed {args.lock}\n  was {before}\n  now {lock['digest']}")
    return 0


def cmd_render(args) -> int:
    lock = lock_mod.load(args.lock)
    written = portage_mod.render(
        lock, args.root, lock_path=args.lock, overlay=args.overlay or None
    )
    for path in written:
        print(path)
    return 0


#: Said once, because it is the only thing `--detach --pretend` needs to hear.
DETACH_PRETEND = (
    "--detach and --pretend are a contradiction. A plan is something you read now, and "
    "a detached job is work nobody is waiting for — there is no plan to come back to. "
    "Pass `--detach --execute` to start the build, or drop --detach to print the plan."
)


def cmd_build(args) -> int:
    # Before the lockfile is even read: a contradiction between two flags is answered
    # from the flags, and "your lockfile is stale" would be an unhelpful thing to say
    # first to somebody who asked for two incompatible things.
    if args.detach and not args.execute:
        raise Fail(DETACH_PRETEND)
    lock = lock_mod.load(args.lock)
    peer = args.peer or os.environ.get("AIOS_BINHOST", "")
    if peer:
        # A peer's binary packages are an accelerator, never an authority: portage
        # still resolves the graph from the rendered lockfile, and --binpkg-respect-use
        # makes it reject any prebuilt package whose flags disagree and build from
        # source instead. So reuse can save time but cannot change what you get.
        #
        # Redacted, because the natural way to produce this argument is
        # `--peer "$(python3 -c 'import aios.mesh; print(aios.mesh.binhost_url())')"`
        # and `binhost_url` says in its own docstring that it is unredacted — the
        # token is in the userinfo. Printing it verbatim would put a live credential
        # on a terminal and, under the agent, into `.aios/agent.jsonl` forever.
        print(f"reusing binary packages from {portage_mod.redact_url(peer)} "
              "where the USE flags match", file=sys.stderr)
    if args.distcc and not _FEATURE_DISTCC.search(_feature_line(lock)):
        # A host list is only half of a distributed build. The other half is whether
        # portage routes compiles through distcc at all, and that half is in the lock
        # already loaded above — so check it rather than reporting a quiet mesh and
        # letting the reader debug the network for a lockfile problem.
        print(
            "the lockfile's FEATURES has no `distcc`, so portage will not route "
            "compiles through it —\nDISTCC_HOSTS is exported and ignored, and "
            "every compile runs locally. Run\n`forge lower && forge render` "
            "first (`forge diff` says whether this lock predates the spec).",
            file=sys.stderr,
        )
    if args.detach:
        return _detach(args, lock, peer)

    argv = portage_mod.emerge_argv(
        lock, root=args.root, pretend=not args.execute, binhost=bool(peer)
    )
    # Both accelerators are environment-only, and for one reason: which machines are
    # up is a fact about this moment. The lockfile refuses either key by name.
    overrides: dict[str, str] = {}
    if peer:
        overrides.update(portage_mod.binhost_env(peer))
    if args.distcc:
        # Imported here, not at module scope: forge is the pure pipeline and has to
        # stay importable on a machine that carries no node code at all. The mesh is
        # asked once, and it never raises — no mesh means an empty host list, which
        # is a local build rather than a failure.
        from aios import mesh as mesh_mod

        view = mesh_mod.look()
        hosts = mesh_mod.distcc_hosts(view)
        overrides.update(portage_mod.distcc_env(hosts))
        print(mesh_mod.distcc_note(hosts, view), file=sys.stderr)
    env = {**os.environ, **overrides} if overrides else None
    if shutil.which("emerge") is None:
        print("emerge is not on PATH — this host cannot build. Would run:")
        print("  " + " ".join(argv))
        return 1
    if not args.execute:
        print("(--pretend; pass --execute to build)", file=sys.stderr)
    print("+ " + " ".join(argv), file=sys.stderr)
    return subprocess.run(argv, env=env).returncode


def _detach(args, lock: dict, peer: str) -> int:
    """Start the build as a detached job and print its id.

    One mechanism, not two: this is the same `aios.build.start` the agent's build_start
    calls, so a job a human began at a shell and a job the agent began are the same kind
    of object, appear in the same registry, and are polled by the same commands. A CLI
    that grew its own private nohup would have been a second answer to the question
    `skills/tool-budget-shorter-than-task` is about, and the second answer is always the
    one nobody keeps working.

    Imported here rather than at module scope for the reason the mesh import above is:
    forge is the pure pipeline, and it has to stay importable on a machine carrying no
    node code at all.
    """
    from aios import build as build_mod

    try:
        job = build_mod.start(
            binhost=peer,
            distcc=args.distcc,
            lock=lock,
            lock_path=args.lock,
            emerge_root=args.root,
        )
    except build_mod.BuildError as exc:
        raise Fail(str(exc)) from None
    # The id alone on stdout, so `id=$(forge build --detach --execute)` is the obvious
    # thing and it works. Everything a human wants to read goes to stderr.
    print(job.id)
    print(
        f"started {job.id} detached — no time limit, and it survives this shell "
        f"closing.\n  log:    {job.log}\n"
        f"  poll:   python3 -m aios.build status {job.id}\n"
        f"  follow: python3 -m aios.build tail {job.id}\n"
        f"  stop:   python3 -m aios.build stop {job.id}",
        file=sys.stderr,
    )
    return 0


def cmd_binpkg(args) -> int:
    lock = lock_mod.load(args.lock)
    sources = list(args.paths)
    if args.dir:
        sources += [str(path) for path in binpkg_mod.walk(args.dir)]
    if args.peer:
        sources += binpkg_mod.peer_sources(args.peer)
    if not sources:
        raise Fail(
            "nothing to check — pass package paths, --dir "
            f"{binpkg_mod.DEFAULT_PKGDIR}, or --peer http://aios-repo:8080/binpkgs"
        )

    fits = [binpkg_mod.examine(source, lock, atom=args.atom or None) for source in sources]
    for line in binpkg_mod.render(fits):
        print(line)
    # Zero is a prediction that emerge will REUSE these, so it is withheld from
    # anything portage would rebuild or a reader should not trust: a mismatch, a
    # foreign build, a package that could not be read, a package whose USE flags are
    # contradicted by what its binaries link, one that disagrees with the ebuild's own
    # IUSE defaults on a flag the lockfile never constrained (`rebuild` — portage
    # recomputes USE from those defaults), and one carrying an enabled feature no
    # intent justified — `--binpkg-respect-use=y` rejects those last two too.
    return 0 if all(item.ok for item in fits) else 1


def cmd_probe(args) -> int:
    names = args.names
    if not names:
        spec = spec_mod.load(args.spec)
        names = list(spec.all_probes())
        if not names:
            raise Fail("no probes named by any intent — add `probes = [...]` to an intent")

    results = probe_mod.run_all(
        names, directory=args.probes, root=args.root, dry_run=args.dry_run
    )
    failed = False
    for result in results:
        print(result.summary())
        for check in result.checks:
            mark = "  ok " if check.passed else "  FAIL"
            detail = f"  ({check.reason})" if check.reason else ""
            print(f"{mark} {check.name}{detail}")
            if args.verbose and not check.passed:
                for stream, text in (("stdout", check.stdout), ("stderr", check.stderr)):
                    if text.strip():
                        print(f"       --- {stream} ---")
                        for line in text.strip().splitlines():
                            print(f"       {line}")
        failed = failed or not result.passed
    return 1 if failed else 0


def cmd_minimize(args) -> int:
    lock = lock_mod.load(args.lock)
    runner: minimize_mod.Runner
    if args.dry_run:
        runner = minimize_mod.DryRunner()
    else:
        runner = minimize_mod.LocalRunner(lock, root=args.root)

    result = minimize_mod.minimize(
        lock,
        args.atom,
        runner,
        probe_dir=args.probes,
        root=args.root,
        levers=args.lever or None,
    )

    for attempt in result.attempts:
        delta = "" if attempt.delta is None else f"  {attempt.delta:+d}B"
        detail = f"  {attempt.detail}" if attempt.detail else ""
        print(f"  {attempt.verdict:<16} -{attempt.lever}{delta}{detail}")
    print(result.summary())
    print(f"journal: {minimize_mod.JOURNAL}")

    if args.apply:
        if result.simulated:
            raise Fail("refusing to --apply a simulated result; drop --dry-run")
        lock_mod.save(minimize_mod.apply(lock, result), args.lock)
        print(f"applied to {args.lock}")
    elif result.dropped:
        print("(--apply to write these into the lockfile)")
    return 0


# --- helpers ----------------------------------------------------------------


#: Whole-word, the same way `probes/distcc.toml` asks the question of `emerge
#: --info`: a `-distcc` negation or a `distcc-pump` entry is not this feature.
_FEATURE_DISTCC = re.compile(r"(?:^|\s)distcc(?:\s|$)")


def _feature_line(lock: dict) -> str:
    """The lockfile's FEATURES value. "" if the lock somehow carries none."""
    for entry in lock["make_conf"]:
        if entry["key"] == "FEATURES":
            return str(entry["value"])
    return ""


def _warn_missing_probes(spec: spec_mod.Spec, directory: str) -> None:
    missing = [
        name for name in spec.all_probes() if not (Path(directory) / f"{name}.toml").is_file()
    ]
    if missing:
        print(
            f"forge: probes named but not defined in {directory}/: {', '.join(missing)}",
            file=sys.stderr,
        )
    naked = [f"intent[{i}]" for i, intent in enumerate(spec.intents) if not intent.probes]
    if naked:
        print(
            f"forge: no probes for {', '.join(naked)} — lowerable, but not minimizable",
            file=sys.stderr,
        )
