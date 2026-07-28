"""What this node is to the rest of the network, as facts rather than belief.

An agent that does not know its own role invents one. #2 held the only ebuild tree
on the network, served it, and published the source releases its peers update from —
and still described serving as something outside its remit, because nothing in its
context said otherwise. The prompt talked about the pipeline; it never said "you are
the node the others depend on".

So the role is not written into the prompt as prose. It is *measured* here, every
session, from state that cannot lie about itself: does a real tree exist, is the port
actually listening, what digest was last published, which peer does this node consume
from, and what the build mesh is offering right now (`aios.mesh`). A briefing
assembled from those facts is right even after the role changes, and it cannot drift
the way a hardcoded paragraph does.

Read-only and cheap. Nothing here starts a service or mints anything — a briefing
that changes the machine by being displayed is not a briefing.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from . import mesh as mesh_mod
from . import repo

SRC = Path("/srv/aios/src")
LATEST = "latest.json"

#: Where a peer reaches this node. The Service name, not a pod IP: which pod is
#: serving is a fact about this moment.
SERVICE = "aios-repo.aios.svc.cluster.local"


@dataclass(frozen=True)
class Role:
    tree: bool = False          # holds a usable ebuild repository
    tree_detail: str = ""
    serving: bool = False       # the HTTP port is actually accepting connections
    binpkgs: int = 0
    published: str = ""         # version of the last release advertised
    peer: str = ""              # where this node takes updates from, if anywhere
    #: What the build mesh offered when this was measured. `None` means it was not
    #: measured at all, which the briefing renders the same way as "no mesh" —
    #: a Role constructed by hand (tests, a caller with facts of its own) must not
    #: be able to trigger a network call by being displayed.
    mesh: mesh_mod.Mesh | None = None

    @property
    def is_seed(self) -> bool:
        """A seed holds the tree and serves it. Both, or it is just a consumer."""
        return self.tree and self.serving


def _listening(port: int = repo.SERVE_PORT) -> bool:
    """Is something answering on the port, here and now.

    Checked by connecting rather than by looking for a process: a dead server with a
    live pidfile is exactly the state an agent must not be told is healthy.
    """
    with socket.socket() as probe:
        probe.settimeout(1.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _published() -> str:
    try:
        return str(json.loads((SRC / LATEST).read_text(encoding="utf-8")).get("version", ""))
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def _binpkgs() -> int:
    try:
        return sum(1 for _ in repo.BINPKGS.rglob("*.gpkg.tar")) + sum(
            1 for _ in repo.BINPKGS.rglob("*.tbz2")
        )
    except OSError:
        return 0


def role(transport: mesh_mod.Transport | None = None) -> Role:
    """Measure this node. One short mesh request; `mesh_mod.look` never raises."""
    tree, detail = repo.looks_real()
    return Role(
        tree=tree,
        tree_detail=detail,
        serving=_listening(),
        binpkgs=_binpkgs(),
        published=_published(),
        peer=os.environ.get("AIOS_UPDATE_URL", ""),
        mesh=mesh_mod.look(transport),
    )


def briefing(current: Role | None = None) -> str:
    """The paragraph appended to the agent's system prompt. Present tense, no hedging."""
    r = current or role()
    lines = ["This node's place in the network, measured at session start:"]

    if r.is_seed:
        lines += [
            "",
            "  YOU ARE THE SEED NODE. You hold the only ebuild tree on this network and",
            f"  you are serving it right now on port {repo.SERVE_PORT}. Other AIos nodes",
            f"  reach you at http://{SERVICE}:{repo.SERVE_PORT}/ and cannot build",
            "  anything without you. Three duties come with that, and they are yours, not",
            "  somebody else's:",
            "",
            f"    tree      /gentoo    {r.tree_detail}",
            f"    binpkgs   /binpkgs   {r.binpkgs} package(s) — what saves a peer a rebuild",
            f"    releases  /src       {'published ' + r.published if r.published else 'NOTHING PUBLISHED YET'}",
            "",
            "  Keeping those current IS the job. Concretely:",
            "    python3 -m aios.repo sync             refresh the tree",
            "    emerge --buildpkg <atom>              build so peers get the binary too",
            "    AIOS_SRC_DIR=/srv/aios/src python3 -m aios.update publish <version>",
            "                                          cut a release peers can update to",
            "",
            "  A peer takes that release with `python3 -m aios.update apply`, which verifies",
            "  the digest, runs the candidate's own tests, and only then swaps it in. So an",
            "  unpublished fix reaches nobody, and a broken one is refused by the peer.",
        ]
        if not r.published:
            lines += [
                "",
                "  Nothing is published. Until you cut a release, no peer can update. Do it.",
            ]
    elif r.tree and not r.serving:
        lines += [
            "",
            f"  You hold a usable ebuild tree ({r.tree_detail}) but you are NOT serving it:",
            f"  nothing is listening on {repo.SERVE_PORT}. If this node is meant to be the",
            "  seed, that is a service you are responsible for starting:",
            "    python3 -m aios.repo serve",
        ]
    else:
        lines += [
            "",
            f"  You are a consumer, not the seed. Your ebuild tree is {r.tree_detail}.",
            f"  {'You take updates from ' + r.peer if r.peer else 'No peer is configured.'}",
            "    python3 -m aios.repo sync        get a tree of your own, or",
            "    python3 -m aios.update check     see what the seed is offering",
        ]

    # What the rest of the network can do FOR this node, as opposed to what this node
    # owes it. An agent that does not know a peer has already compiled the package it
    # is about to compile will compile it — the briefing is the only place it finds out.
    lines += ["", mesh_mod.briefing(r.mesh)]

    lines += [
        "",
        "  Wrong-shaped answer: 'serving the tree is handled separately'. Nothing about",
        "  this node is handled separately. Check the facts above before you claim a",
        "  capability is missing — they were read from this machine seconds ago.",
    ]
    return "\n".join(lines)


def main() -> int:
    print(briefing())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
