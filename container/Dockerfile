# An AIos machine.
#
# Base is Gentoo's own arm64/musl/openrc stage3 — pinned by date, because a
# floating base tag would make the image unreproducible and this project's whole
# claim is that the machine is reproducible from its lockfile. stage3 *is* the
# minimal Gentoo install: a toolchain, portage, and python3. Nothing else.
#
# No package is emerged at build time. That is deliberate: what gets compiled is
# decided by aios.lock.json at runtime, by the agent, not baked in here by hand.
FROM gentoo/stage3:arm64-musl-openrc-20260727

# Root with no password. This is a single-tenant dev machine reached only through
# `kubectl exec` — the API server is the authentication boundary, and a password
# on top of it would be a password nobody types.
RUN passwd -d root \
    || sed -i 's|^root:[^:]*:|root::|' /etc/shadow

ENV AIOS_ROOT=/aios \
    PYTHONPATH=/aios \
    PYTHONDONTWRITEBYTECODE=1 \
    TERM=xterm-256color \
    COLORTERM=truecolor

WORKDIR /aios

# The build tool and the machine's own description. Copied before the userland so
# that iterating on the welcome screen and the agent does not invalidate this layer.
COPY forge/ /aios/forge/
COPY probes/ /aios/probes/
COPY overlay/ /aios/overlay/
COPY aios.toml aios.lock.json /aios/

# The machine ships its own tests, design docs and skills. This is not padding:
# an AIos node is expected to rebuild itself, so the agent living inside it needs
# to be able to read why the thing is built the way it is, and to verify the build
# tool still works after it has edited it. A self-modifying machine that cannot run
# its own test suite is just a machine that breaks quietly.
COPY tests/ /aios/tests/
COPY skills/ /aios/skills/
COPY README.md DESIGN.md MANUAL.md /aios/

# Boot and login paths.
# The container/ sources also land INSIDE /aios, not only at their install paths.
#
# aios.update.PAYLOAD lists `container`, because an update has to be able to deliver
# the boot path (/sbin/aios-init), the login path and the tmux layout — INSTALL copies
# them out of the payload after a successful swap. Copying these files only to their
# destinations left /aios/container absent, so `aios.update publish` failed outright
# with "cannot publish, missing from /aios: container" and this node could not serve
# its own code to the mesh at all. A node that can take updates but never publish one
# is only half of the A/B story in DESIGN.md §5.
COPY container/ /aios/container/
COPY container/aios-init /sbin/aios-init
COPY container/aios-login /sbin/aios-login
COPY container/manual /usr/local/bin/manual
COPY container/aios-cockpit /usr/local/bin/aios-cockpit
# Machine-owned, loaded with `tmux -f`, so the two-pane layout intent[3] asks for is
# a property of the machine and not of whoever last edited a dotfile.
COPY container/tmux.conf /etc/aios/tmux.conf
RUN chmod +x /sbin/aios-init /sbin/aios-login /usr/local/bin/manual /usr/local/bin/aios-cockpit \
    && ln -sf /sbin/aios-login /usr/local/bin/aios \
    # A real `forge` on PATH, not just the shell alias in profile.d. The agent's
    # run_shell tool executes a non-interactive shell that never sources profile,
    # so `forge reseal` came back "command not found" and it started hand-patching
    # generated files instead.
    && printf '%s\n' '#!/bin/sh' 'exec python3 -m forge "$@"' > /usr/local/bin/forge \
    && chmod +x /usr/local/bin/forge

# A prompt that matches the rest of the machine, for when you want a plain shell
# instead of the agent.
RUN printf '%s\n' \
    'export PYTHONPATH=/aios' \
    'export PATH="/usr/local/bin:$PATH"' \
    "PS1='\\[\\033[38;5;117m\\]aios\\[\\033[2m\\]:\\w\\[\\033[0m\\] \\$ '" \
    'alias forge="python3 -m forge"' \
    > /etc/profile.d/aios.sh

# The userland: welcome screen and agent loop. Last, because it changes most.
COPY aios/ /aios/aios/

# Fail the build rather than ship a machine whose userland does not import.
RUN python3 -c "import forge, aios.welcome, aios.agent, aios.tools, aios.llm" \
    && python3 -m forge --lock /aios/aios.lock.json show > /dev/null \
    # Every suite the image carries, not a subset: an image that ships a broken
    # cockpit or mesh client is as unusable as one with a broken build tool, and the
    # only cheap moment to find out is here.
    && python3 -m aios.update gate /aios

LABEL org.opencontainers.image.title="AIos" \
      org.opencontainers.image.description="A Linux that compiles itself from stated intent." \
      org.opencontainers.image.base.name="gentoo/stage3:arm64-musl-openrc-20260727"

CMD ["/sbin/aios-init"]
