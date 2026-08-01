"""BIP-340 Schnorr signatures over secp256k1, in the standard library alone.

Nostr — and therefore the buzz relay this machine registers on — identifies every
participant by a 32-byte x-only secp256k1 public key and authenticates every event
with a BIP-340 Schnorr signature. There is no way to say "hello, I am this node" to
a Nostr relay without being able to produce one.

WHY THIS IS WRITTEN OUT BY HAND rather than imported. AIos targets a bare
aarch64/musl Gentoo whose only interpreter is portage's python3, and the project
forbids pip: a machine whose description is `aios.lock.json` cannot depend on
packages that lockfile does not name. `coincurve`, `secp256k1`, `ecdsa` and friends
are all off the table, and `hashlib` is the only cryptographic primitive the stdlib
offers. So the curve arithmetic is here, following the reference implementation in
BIP-340 itself closely enough to be checked against it line by line — deliberately
NOT restructured for elegance, because the only review that matters for this file is
"does it match the BIP".

Correctness is not argued, it is measured: aios/test_bip340.py runs all 19 official
BIP-340 test vectors, including every negative one. Nothing else here is trustworthy
without that, and a change to this file that does not keep those green is wrong no
matter how good it looks.

    KNOWN LIMITATION, stated because a reader deserves it up front: this is NOT
    constant-time and cannot be. Python's big integers branch and allocate on
    value, `pow()` makes no timing promise, and the secret scalar drives the
    multiply loop. A local attacker who can time this process precisely could in
    principle recover the node key.

    That is an accepted risk here and not elsewhere: the key identifies an AIos
    node to its own mesh, on a local trusted network, and it is stored at
    /aios/.aios/buzz-key on the state volume. Anyone positioned to time this
    process can already read that file, so the timing channel grants nothing new.
    Do not lift this module into a context where the key is worth money.
"""

from __future__ import annotations

import hashlib
import os

#: secp256k1. The field, the group order, and the generator — the standard domain
#: parameters, spelled out rather than derived so they can be eyeballed against SEC 2.
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)

#: A curve point as (x, y), or None for the point at infinity.
#:
#: `None` rather than a sentinel object because BIP-340's verification has a case
#: that turns on it: test vectors 9 and 10 are forgeries that succeed against any
#: implementation which lets the infinite point answer has_even_y() or x(). Making
#: infinity unrepresentable as a pair means those questions cannot be asked of it
#: by accident — they raise instead.
Point = tuple[int, int] | None

KEY_BYTES = 32
SIG_BYTES = 64
AUX_BYTES = 32


class SigningError(Exception):
    """Message is user-facing and must never quote key material."""


def _tagged_hash(tag: str, msg: bytes) -> bytes:
    """BIP-340's domain separation: sha256(sha256(tag) || sha256(tag) || msg).

    The tag hash is duplicated on purpose (it makes the prefix exactly one SHA-256
    block, so the tag costs nothing at hash time). Getting this wrong produces
    signatures that verify against themselves and against nothing else, which is the
    worst possible failure mode — hence the vectors.
    """
    prefix = hashlib.sha256(tag.encode("utf-8")).digest()
    return hashlib.sha256(prefix + prefix + msg).digest()


def _x(point: Point) -> int:
    assert point is not None
    return point[0]


def _y(point: Point) -> int:
    assert point is not None
    return point[1]


def _has_even_y(point: Point) -> bool:
    assert point is not None, "the infinite point has no y; callers must check first"
    return _y(point) % 2 == 0


def _point_add(p1: Point, p2: Point) -> Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    if _x(p1) == _x(p2) and _y(p1) != _y(p2):
        return None
    if p1 == p2:
        lam = 3 * _x(p1) * _x(p1) * pow(2 * _y(p1), P - 2, P) % P
    else:
        lam = (_y(p2) - _y(p1)) * pow(_x(p2) - _x(p1), P - 2, P) % P
    x3 = (lam * lam - _x(p1) - _x(p2)) % P
    return (x3, (lam * (_x(p1) - x3) - _y(p1)) % P)


def _point_mul(point: Point, scalar: int) -> Point:
    """Double-and-add, least significant bit first. 256 rounds, always."""
    result: Point = None
    running = point
    for i in range(256):
        if (scalar >> i) & 1:
            result = _point_add(result, running)
        running = _point_add(running, running)
    return result


def _double_g() -> tuple:
    """G, 2G, 4G, ... 2^255·G, computed once at import.

    Every scalar multiplication in this module except one is by the GENERATOR: two in
    `sign` and one of the two in `verify`. Each was re-deriving the same 256 doublings
    from scratch, which is most of the work — signing is ~40% cheaper with the chain
    precomputed, and this module is now on the release gate's critical path twice
    (aios/test_bip340.py and aios/test_buzz.py sign a few hundred times between them).

    Purely a cache: `_point_mul_g(k)` and `_point_mul(G, k)` return the same point for
    every k, and the official vectors are what proves it.
    """
    table = []
    running: Point = G
    for _ in range(256):
        table.append(running)
        running = _point_add(running, running)
    return tuple(table)


_G_MULTIPLES = _double_g()


def _point_mul_g(scalar: int) -> Point:
    """scalar·G, using the precomputed doubling chain."""
    result: Point = None
    for i in range(256):
        if (scalar >> i) & 1:
            result = _point_add(result, _G_MULTIPLES[i])
    return result


def _lift_x(raw: bytes) -> Point:
    """The even-y curve point with this x coordinate, or None if there is none.

    This is what makes a 32-byte "x-only" public key meaningful: of the two points
    sharing an x, BIP-340 always means the one with even y. Returns None both for an
    x at or above the field size and for an x that is not on the curve at all —
    vectors 5 and 14 are exactly those two cases.
    """
    x = int.from_bytes(raw, "big")
    if x >= P:
        return None
    y_squared = (pow(x, 3, P) + 7) % P
    y = pow(y_squared, (P + 1) // 4, P)
    if pow(y, 2, P) != y_squared:
        return None
    return (x, y if y % 2 == 0 else P - y)


def _bytes_from_int(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _bytes_from_point(point: Point) -> bytes:
    return _bytes_from_int(_x(point))


def seckey_valid(seckey: bytes) -> bool:
    """Is this 32 bytes a usable secret key: on [1, N-1].

    Both ends are real exclusions rather than pedantry. Zero has no public key, and
    a scalar at or above the group order is a different key than it looks like — it
    reduces, so two distinct "keys" would sign as one identity.
    """
    if len(seckey) != KEY_BYTES:
        return False
    return 1 <= int.from_bytes(seckey, "big") <= N - 1


def pubkey(seckey: bytes) -> bytes:
    """The 32-byte x-only public key for this secret key."""
    if not seckey_valid(seckey):
        raise SigningError("secret key must be 32 bytes on [1, N-1]")
    point = _point_mul_g(int.from_bytes(seckey, "big"))
    return _bytes_from_point(point)


def sign(msg: bytes, seckey: bytes, aux_rand: bytes | None = None) -> bytes:
    """A 64-byte BIP-340 signature over `msg`.

    `msg` is any length: BIP-340 was extended in 2022 to drop the 32-byte
    restriction, and vectors 15-18 cover 0, 1, 17 and 100 bytes. Nostr only ever
    signs a 32-byte event id, but refusing the other lengths would mean failing four
    official vectors, and a signer that cannot pass its own spec's tests is not one
    worth trusting with the four that matter.

    `aux_rand` defaults to fresh randomness, which BIP-340 recommends as side-channel
    hardening. Passing it explicitly makes signing deterministic, which is what the
    vectors need — and note that determinism is a TEST affordance, not a feature to
    reach for in production: reusing aux_rand across different messages is harmless,
    but the parameter exists so tests can be exact, not so callers can be tidy.

    Signature verification runs before returning, as the BIP advises. It doubles the
    cost of signing and it is worth it: a faulty signature detected here is an
    exception, while one that escapes is a node the relay silently refuses.
    """
    if not seckey_valid(seckey):
        raise SigningError("secret key must be 32 bytes on [1, N-1]")
    if aux_rand is None:
        aux_rand = os.urandom(AUX_BYTES)
    if len(aux_rand) != AUX_BYTES:
        raise SigningError(f"aux_rand must be exactly {AUX_BYTES} bytes")

    d0 = int.from_bytes(seckey, "big")
    point = _point_mul_g(d0)
    # The signing scalar is negated when the public point has odd y, which is what
    # makes an x-only public key sufficient to verify against.
    d = d0 if _has_even_y(point) else N - d0

    tweak = d ^ int.from_bytes(_tagged_hash("BIP0340/aux", aux_rand), "big")
    rand = _tagged_hash(
        "BIP0340/nonce", _bytes_from_int(tweak) + _bytes_from_point(point) + msg
    )
    k0 = int.from_bytes(rand, "big") % N
    if k0 == 0:
        # Unreachable short of a broken hash; raising beats emitting a zero nonce,
        # which would publish the secret key to anyone who looked.
        raise SigningError("nonce was zero; refusing to sign")

    nonce_point = _point_mul_g(k0)
    k = k0 if _has_even_y(nonce_point) else N - k0
    challenge = int.from_bytes(
        _tagged_hash(
            "BIP0340/challenge",
            _bytes_from_point(nonce_point) + _bytes_from_point(point) + msg,
        ),
        "big",
    ) % N
    signature = _bytes_from_point(nonce_point) + _bytes_from_int((k + challenge * d) % N)

    if not verify(msg, _bytes_from_point(point), signature):
        raise SigningError("produced a signature that does not verify; refusing to use it")
    return signature


def verify(msg: bytes, pub: bytes, signature: bytes) -> bool:
    """Does `signature` verify as `pub`'s over `msg`. False, never an exception.

    Returning False for malformed input rather than raising is deliberate: every
    caller of this is deciding whether to trust something that arrived from outside,
    and a distinction between "invalid" and "unparseable" is one they do not have a
    different answer for. The negative vectors (5-14) are all of this shape.
    """
    if len(pub) != KEY_BYTES or len(signature) != SIG_BYTES:
        return False
    point = _lift_x(pub)
    if point is None:
        return False

    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if r >= P or s >= N:
        return False

    challenge = int.from_bytes(
        _tagged_hash("BIP0340/challenge", signature[:32] + pub + msg), "big"
    ) % N
    # sG - eP. Written as an addition with the negated scalar because the group has
    # no subtraction; N - challenge is that negation.
    recovered = _point_add(_point_mul_g(s), _point_mul(point, N - challenge))
    if recovered is None:
        return False  # vectors 9 and 10: the infinite point is not a valid R
    return _has_even_y(recovered) and _x(recovered) == r
