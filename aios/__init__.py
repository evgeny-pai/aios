"""The AIos userland — the machine's own agent, and the screen that greets you.

`forge` is the build tool: it lowers intent into a lockfile and renders portage
state. This package is the thing a *person* talks to. It drives forge, tests its
own work with the probes, and writes every step it takes to `.aios/agent.jsonl`
so a run can be audited after the fact.

Stdlib only, like the rest of the tree: the target is a bare aarch64/musl Gentoo
box whose only interpreter is the python3 portage already requires.
"""

__version__ = "0.1.0"
