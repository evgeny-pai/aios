"""A chain of backends: ask the local model first, fall back to the hosted one.

The lowering pass is the one nondeterministic step in the pipeline and the only one
that needs a model at all, so it is also the only place where "which model" is worth
making a policy rather than a setting. The policy this implements is the same one
ConductorAI applies to its arbiter, for the same reasons: a model running on the LAN
costs nothing per call and keeps the spec on the premises, so it should be asked
first; a hosted model is better at the job, so it should be there when the local one
is absent, unloaded, or returns something that does not validate.

Falling back on ProviderError specifically is what makes this safe. That error is
raised for an unreachable daemon, an HTTP error, a timeout, and — importantly — a
response that could not be parsed into the requested schema. A local model that
answers confidently with malformed JSON is therefore treated exactly like one that is
switched off, which is the behaviour you want from a step whose output becomes a
committed artifact.

What it deliberately does NOT do is merge or compare answers. The lockfile is a
reviewable diff produced by ONE backend, and `generated_by` in the lock records which
one; a lock stitched together from two models would be attributable to neither.
"""

from __future__ import annotations

import os
import sys

from . import Provider, ProviderError

#: Local first, hosted second. Overridable so a machine with no local daemon can put
#: the hosted backend first without editing the spec.
DEFAULT_CHAIN = "ollama,anthropic"


def _model_for(link: str, spec_provider: str, spec_model: str) -> str:
    """Per-link model, because the chain spans backends with different model names.

    A single AIOS_MODEL cannot serve both links — `qwen3:14b` means nothing to the
    hosted API and `claude-haiku-4-5` means nothing to Ollama. So each link reads its
    own AIOS_<LINK>_MODEL, and the spec's `agent.model` applies only to the link the
    spec actually named.
    """
    explicit = os.environ.get(f"AIOS_{link.upper()}_MODEL")
    if explicit:
        return explicit
    if link == spec_provider:
        return spec_model
    return ""


class FallbackProvider:
    def __init__(self, chain: list[Provider]) -> None:
        if not chain:
            raise ProviderError("the fallback chain is empty")
        self.name = "fallback"
        self.chain = chain
        # The first link is the intended backend, so it names the provider. `answered`
        # is what actually served the call, which is the interesting fact afterwards.
        self.model = chain[0].model
        self.answered: Provider | None = None

    def describe(self) -> str:
        return "fallback[" + " -> ".join(p.describe() for p in self.chain) + "]"

    def retire_answering_link(self) -> bool:
        """Drop the link that just answered; True if another one remains.

        For the caller that can tell a bad answer from a failed call. A backend that
        returns a well-formed response the pipeline then rejects has not raised
        anything, so the chain cannot see it — `forge.lower` validates the payload and
        calls this when it does not hold up, which is what lets a local model be tried
        first without letting it break the lowering.
        """
        if self.answered is not None and self.answered in self.chain:
            self.chain.remove(self.answered)
            self.answered = None
        if not self.chain:
            return False
        self.model = self.chain[0].model
        return True

    def complete_json(self, *, system: str, prompt: str, schema: dict) -> dict:
        failures = []
        for index, provider in enumerate(self.chain):
            try:
                result = provider.complete_json(system=system, prompt=prompt, schema=schema)
            except ProviderError as exc:
                failures.append(f"  {provider.describe()}: {exc}")
                # Loud on stderr, not silent: a lowering that quietly cost money
                # because the local daemon was down is a surprise on the invoice, and
                # one that quietly used a weaker model is a surprise in the diff.
                remaining = len(self.chain) - index - 1
                print(
                    f"fallback: {provider.describe()} did not answer ({exc}); "
                    + (f"trying {self.chain[index + 1].describe()}" if remaining else "no backend left"),
                    file=sys.stderr,
                )
                continue
            self.answered = provider
            self.model = provider.model
            if index:
                print(f"fallback: answered by {provider.describe()}", file=sys.stderr)
            return result
        raise ProviderError(
            "every backend in the fallback chain failed:\n" + "\n".join(failures)
        )


def build(spec_provider: str, spec_model: str, *, effort: str) -> FallbackProvider:
    """Assemble the chain named by AIOS_FALLBACK_CHAIN.

    A link that cannot even be constructed — the ollama backend with no model set, a
    hosted backend with no credentials — is skipped with a note rather than raised,
    because the whole point of a chain is to tolerate a missing link. Only an empty
    chain is an error.
    """
    from . import load

    links: list[Provider] = []
    names = [n.strip() for n in os.environ.get("AIOS_FALLBACK_CHAIN", DEFAULT_CHAIN).split(",")]
    for name in names:
        if not name or name == "fallback":  # no recursion
            continue
        try:
            links.append(
                load(
                    name,
                    _model_for(name, spec_provider, spec_model),
                    effort=effort,
                    respect_env=False,
                )
            )
        except ProviderError as exc:
            print(f"fallback: {name} unavailable, skipping ({exc})", file=sys.stderr)
    if not links:
        raise ProviderError(
            f"no usable backend in AIOS_FALLBACK_CHAIN={names!r} — "
            "set AIOS_OLLAMA_MODEL for a local model, or ANTHROPIC_API_KEY for the hosted one"
        )
    return FallbackProvider(links)
