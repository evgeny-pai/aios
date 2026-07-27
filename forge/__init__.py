"""forge — AIos's build tool.

Pipeline: aios.toml --lower--> aios.lock.json --render--> /etc/portage --emerge--> system.
The lowering pass is the only step that talks to a model; everything after it
reads the lockfile and nothing else.

Stdlib only, on purpose: this has to run on a bare aarch64/musl target whose only
interpreter is the python3 portage already requires.
"""

__version__ = "0.1.0"
