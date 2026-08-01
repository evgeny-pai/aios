"""The official BIP-340 test vectors, all nineteen, run against aios.bip340.

This suite is the entire reason to believe the hand-written signer in aios/bip340.py.
There is no live relay in a unit test and no reference library to compare against, so
"our signatures verify against our verifier" would be worth nothing — a consistently
wrong implementation passes that. What cannot be faked is reproducing byte-for-byte
the signatures the BIP itself publishes, and rejecting the forgeries it publishes.

The table below is GENERATED from bitcoin/bips bip-0340/test-vectors.csv rather than
typed, because nineteen rows of 64-character hex is exactly the kind of thing that
acquires a silent transcription error and then proves the wrong claim forever.

Vectors 0-3 and 15-18 carry a secret key, so both signing and verification are
checked against them; the signature must come out EQUAL to the published one, not
merely valid. Vectors 4-14 are verification-only, and 5-14 are forgeries that must be
rejected — each for a different reason, named in its comment. Those ten are the
valuable half of the file: an implementation that accepts any one of them is one an
attacker can impersonate this node through.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from . import bip340


@dataclass(frozen=True)
class Vector:
    index: int
    seckey: str
    pubkey: str
    aux: str
    msg: str
    sig: str
    valid: bool
    comment: str

    @property
    def signable(self) -> bool:
        """Only vectors carrying a secret key can exercise the signer."""
        return bool(self.seckey)


VECTORS = (
    # 0
    Vector(
        index=0,
        seckey="0000000000000000000000000000000000000000000000000000000000000003",
        pubkey="F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9",
        aux="0000000000000000000000000000000000000000000000000000000000000000",
        msg="0000000000000000000000000000000000000000000000000000000000000000",
        sig="E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA821525F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0",
        valid=True,
        comment="",
    ),
    # 1
    Vector(
        index=1,
        seckey="B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF",
        pubkey="DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        aux="0000000000000000000000000000000000000000000000000000000000000001",
        msg="243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        sig="6896BD60EEAE296DB48A229FF71DFE071BDE413E6D43F917DC8DCF8C78DE33418906D11AC976ABCCB20B091292BFF4EA897EFCB639EA871CFA95F6DE339E4B0A",
        valid=True,
        comment="",
    ),
    # 2
    Vector(
        index=2,
        seckey="C90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B14E5C9",
        pubkey="DD308AFEC5777E13121FA72B9CC1B7CC0139715309B086C960E18FD969774EB8",
        aux="C87AA53824B4D7AE2EB035A2B5BBBCCC080E76CDC6D1692C4B0B62D798E6D906",
        msg="7E2D58D8B3BCDF1ABADEC7829054F90DDA9805AAB56C77333024B9D0A508B75C",
        sig="5831AAEED7B44BB74E5EAB94BA9D4294C49BCF2A60728D8B4C200F50DD313C1BAB745879A5AD954A72C45A91C3A51D3C7ADEA98D82F8481E0E1E03674A6F3FB7",
        valid=True,
        comment="",
    ),
    # 3: test fails if msg is reduced modulo p or n
    Vector(
        index=3,
        seckey="0B432B2677937381AEF05BB02A66ECD012773062CF3FA2549E44F58ED2401710",
        pubkey="25D1DFF95105F5253C4022F628A996AD3A0D95FBF21D468A1B33F8C160D8F517",
        aux="FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
        msg="FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
        sig="7EB0509757E246F19449885651611CB965ECC1A187DD51B64FDA1EDC9637D5EC97582B9CB13DB3933705B32BA982AF5AF25FD78881EBB32771FC5922EFC66EA3",
        valid=True,
        comment="test fails if msg is reduced modulo p or n",
    ),
    # 4
    Vector(
        index=4,
        seckey="",
        pubkey="D69C3509BB99E412E68B0FE8544E72837DFA30746D8BE2AA65975F29D22DC7B9",
        aux="",
        msg="4DF3C3F68FCC83B27E9D42C90431A72499F17875C81A599B566C9889B9696703",
        sig="00000000000000000000003B78CE563F89A0ED9414F5AA28AD0D96D6795F9C6376AFB1548AF603B3EB45C9F8207DEE1060CB71C04E80F593060B07D28308D7F4",
        valid=True,
        comment="",
    ),
    # 5: public key not on the curve
    Vector(
        index=5,
        seckey="",
        pubkey="EEFDEA4CDB677750A420FEE807EACF21EB9898AE79B9768766E4FAA04A2D4A34",
        aux="",
        msg="243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        sig="6CFF5C3BA86C69EA4B7376F31A9BCB4F74C1976089B2D9963DA2E5543E17776969E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B",
        valid=False,
        comment="public key not on the curve",
    ),
    # 6: has_even_y(R) is false
    Vector(
        index=6,
        seckey="",
        pubkey="DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        aux="",
        msg="243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        sig="FFF97BD5755EEEA420453A14355235D382F6472F8568A18B2F057A14602975563CC27944640AC607CD107AE10923D9EF7A73C643E166BE5EBEAFA34B1AC553E2",
        valid=False,
        comment="has_even_y(R) is false",
    ),
    # 7: negated message
    Vector(
        index=7,
        seckey="",
        pubkey="DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        aux="",
        msg="243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        sig="1FA62E331EDBC21C394792D2AB1100A7B432B013DF3F6FF4F99FCB33E0E1515F28890B3EDB6E7189B630448B515CE4F8622A954CFE545735AAEA5134FCCDB2BD",
        valid=False,
        comment="negated message",
    ),
    # 8: negated s value
    Vector(
        index=8,
        seckey="",
        pubkey="DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        aux="",
        msg="243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        sig="6CFF5C3BA86C69EA4B7376F31A9BCB4F74C1976089B2D9963DA2E5543E177769961764B3AA9B2FFCB6EF947B6887A226E8D7C93E00C5ED0C1834FF0D0C2E6DA6",
        valid=False,
        comment="negated s value",
    ),
    # 9: sG - eP is infinite. Test fails in single verification if has_even_y(inf) is defined as true and x(inf) as 0
    Vector(
        index=9,
        seckey="",
        pubkey="DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        aux="",
        msg="243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        sig="0000000000000000000000000000000000000000000000000000000000000000123DDA8328AF9C23A94C1FEECFD123BA4FB73476F0D594DCB65C6425BD186051",
        valid=False,
        comment="sG - eP is infinite. Test fails in single verification if has_even_y(inf) is defined as true and x(inf) as 0",
    ),
    # 10: sG - eP is infinite. Test fails in single verification if has_even_y(inf) is defined as true and x(inf) as 1
    Vector(
        index=10,
        seckey="",
        pubkey="DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        aux="",
        msg="243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        sig="00000000000000000000000000000000000000000000000000000000000000017615FBAF5AE28864013C099742DEADB4DBA87F11AC6754F93780D5A1837CF197",
        valid=False,
        comment="sG - eP is infinite. Test fails in single verification if has_even_y(inf) is defined as true and x(inf) as 1",
    ),
    # 11: sig[0:32] is not an X coordinate on the curve
    Vector(
        index=11,
        seckey="",
        pubkey="DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        aux="",
        msg="243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        sig="4A298DACAE57395A15D0795DDBFD1DCB564DA82B0F269BC70A74F8220429BA1D69E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B",
        valid=False,
        comment="sig[0:32] is not an X coordinate on the curve",
    ),
    # 12: sig[0:32] is equal to field size
    Vector(
        index=12,
        seckey="",
        pubkey="DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        aux="",
        msg="243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        sig="FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F69E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B",
        valid=False,
        comment="sig[0:32] is equal to field size",
    ),
    # 13: sig[32:64] is equal to curve order
    Vector(
        index=13,
        seckey="",
        pubkey="DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        aux="",
        msg="243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        sig="6CFF5C3BA86C69EA4B7376F31A9BCB4F74C1976089B2D9963DA2E5543E177769FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141",
        valid=False,
        comment="sig[32:64] is equal to curve order",
    ),
    # 14: public key is not a valid X coordinate because it exceeds the field size
    Vector(
        index=14,
        seckey="",
        pubkey="FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC30",
        aux="",
        msg="243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        sig="6CFF5C3BA86C69EA4B7376F31A9BCB4F74C1976089B2D9963DA2E5543E17776969E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B",
        valid=False,
        comment="public key is not a valid X coordinate because it exceeds the field size",
    ),
    # 15: message of size 0 (added 2022-12)
    Vector(
        index=15,
        seckey="0340034003400340034003400340034003400340034003400340034003400340",
        pubkey="778CAA53B4393AC467774D09497A87224BF9FAB6F6E68B23086497324D6FD117",
        aux="0000000000000000000000000000000000000000000000000000000000000000",
        msg="",
        sig="71535DB165ECD9FBBC046E5FFAEA61186BB6AD436732FCCC25291A55895464CF6069CE26BF03466228F19A3A62DB8A649F2D560FAC652827D1AF0574E427AB63",
        valid=True,
        comment="message of size 0 (added 2022-12)",
    ),
    # 16: message of size 1 (added 2022-12)
    Vector(
        index=16,
        seckey="0340034003400340034003400340034003400340034003400340034003400340",
        pubkey="778CAA53B4393AC467774D09497A87224BF9FAB6F6E68B23086497324D6FD117",
        aux="0000000000000000000000000000000000000000000000000000000000000000",
        msg="11",
        sig="08A20A0AFEF64124649232E0693C583AB1B9934AE63B4C3511F3AE1134C6A303EA3173BFEA6683BD101FA5AA5DBC1996FE7CACFC5A577D33EC14564CEC2BACBF",
        valid=True,
        comment="message of size 1 (added 2022-12)",
    ),
    # 17: message of size 17 (added 2022-12)
    Vector(
        index=17,
        seckey="0340034003400340034003400340034003400340034003400340034003400340",
        pubkey="778CAA53B4393AC467774D09497A87224BF9FAB6F6E68B23086497324D6FD117",
        aux="0000000000000000000000000000000000000000000000000000000000000000",
        msg="0102030405060708090A0B0C0D0E0F1011",
        sig="5130F39A4059B43BC7CAC09A19ECE52B5D8699D1A71E3C52DA9AFDB6B50AC370C4A482B77BF960F8681540E25B6771ECE1E5A37FD80E5A51897C5566A97EA5A5",
        valid=True,
        comment="message of size 17 (added 2022-12)",
    ),
    # 18: message of size 100 (added 2022-12)
    Vector(
        index=18,
        seckey="0340034003400340034003400340034003400340034003400340034003400340",
        pubkey="778CAA53B4393AC467774D09497A87224BF9FAB6F6E68B23086497324D6FD117",
        aux="0000000000000000000000000000000000000000000000000000000000000000",
        msg="99999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999",
        sig="403B12B0D8555A344175EA7EC746566303321E5DBFA8BE6F091635163ECA79A8585ED3E3170807E7C03B720FC54C7B23897FCBA0E9D0B4A06894CFD249F22367",
        valid=True,
        comment="message of size 100 (added 2022-12)",
    ),
)


SIGNABLE = tuple(v for v in VECTORS if v.signable)
FORGERIES = tuple(v for v in VECTORS if not v.valid)


class TestOfficialVectors(unittest.TestCase):
    """Every published vector, each as its own assertion with its own message."""

    def test_derives_the_published_public_key(self):
        for v in SIGNABLE:
            with self.subTest(vector=v.index):
                self.assertEqual(
                    bip340.pubkey(bytes.fromhex(v.seckey)).hex().upper(),
                    v.pubkey,
                    f"vector {v.index}: derived public key differs from the BIP's",
                )

    def test_reproduces_the_published_signature_exactly(self):
        for v in SIGNABLE:
            with self.subTest(vector=v.index):
                produced = bip340.sign(
                    bytes.fromhex(v.msg),
                    bytes.fromhex(v.seckey),
                    bytes.fromhex(v.aux),
                )
                self.assertEqual(
                    produced.hex().upper(),
                    v.sig,
                    f"vector {v.index} ({v.comment or 'no comment'}): signature differs",
                )

    def test_verification_matches_the_published_verdict(self):
        for v in VECTORS:
            with self.subTest(vector=v.index, expect=v.valid):
                self.assertEqual(
                    bip340.verify(
                        bytes.fromhex(v.msg),
                        bytes.fromhex(v.pubkey),
                        bytes.fromhex(v.sig),
                    ),
                    v.valid,
                    f"vector {v.index}: {v.comment or 'expected verdict not produced'}",
                )


class TestTheSuiteIsNotVacuous(unittest.TestCase):
    """Guards on the table itself.

    skills/vacuous-probe-checks exists because two probe checks here once passed with
    the binary absent. The same trap is available to a vector table: a filter typo, a
    generator change, or a well-meaning "clean up the fixtures" commit can leave a
    suite that iterates over nothing and reports green. These assertions fail loudly
    in that case instead.
    """

    def test_the_table_is_the_full_published_set(self):
        self.assertEqual(len(VECTORS), 19, "BIP-340 publishes 19 vectors")
        self.assertEqual([v.index for v in VECTORS], list(range(19)), "indices must be 0..18 in order")

    def test_forgeries_are_actually_present(self):
        # The whole value of this file is the negative cases. If the table ever holds
        # only valid signatures, `test_verification_matches` degenerates into "our
        # verifier accepts our own output".
        self.assertEqual(len(FORGERIES), 10, "vectors 5-14 are the forgeries")
        self.assertEqual([v.index for v in FORGERIES], list(range(5, 15)))
        for v in FORGERIES:
            with self.subTest(vector=v.index):
                self.assertTrue(v.comment, f"forgery {v.index} must say what it attacks")

    def test_signable_vectors_cover_the_variable_length_extension(self):
        lengths = sorted({len(bytes.fromhex(v.msg)) for v in SIGNABLE})
        self.assertEqual(
            lengths, [0, 1, 17, 32, 100],
            "the 2022 variable-length extension vectors (0, 1, 17, 100 bytes) must be exercised",
        )


class TestSignerRejectsBadInput(unittest.TestCase):
    """Boundaries the vectors do not cover, because they are about the API not the maths."""

    VALID = bytes.fromhex("B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF")

    def test_secret_key_must_be_in_range(self):
        for name, key in (
            ("zero", b"\x00" * 32),
            ("group order", bip340.N.to_bytes(32, "big")),
            ("above the group order", (bip340.N + 1).to_bytes(32, "big")),
            ("too short", b"\x01" * 31),
            ("too long", b"\x01" * 33),
        ):
            with self.subTest(key=name):
                self.assertFalse(bip340.seckey_valid(key))
                with self.assertRaises(bip340.SigningError):
                    bip340.sign(b"\x00" * 32, key, b"\x00" * 32)
                with self.assertRaises(bip340.SigningError):
                    bip340.pubkey(key)

    def test_aux_rand_must_be_32_bytes(self):
        with self.assertRaises(bip340.SigningError):
            bip340.sign(b"\x00" * 32, self.VALID, b"\x00" * 31)

    def test_a_flipped_bit_anywhere_breaks_the_signature(self):
        """The positive assertion above is only meaningful if verification is tight."""
        msg = bytes.fromhex("243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89")
        pub = bip340.pubkey(self.VALID)
        sig = bip340.sign(msg, self.VALID, b"\x00" * 32)
        self.assertTrue(bip340.verify(msg, pub, sig))

        for position in (0, 31, 32, 63):
            with self.subTest(byte=position):
                broken = bytearray(sig)
                broken[position] ^= 0x01
                self.assertFalse(bip340.verify(msg, pub, bytes(broken)))

        flipped_msg = bytearray(msg)
        flipped_msg[0] ^= 0x01
        self.assertFalse(bip340.verify(bytes(flipped_msg), pub, sig))

    def test_signing_without_aux_rand_still_verifies(self):
        """The production path: aux_rand omitted, so os.urandom supplies it."""
        msg = b"\x07" * 32
        pub = bip340.pubkey(self.VALID)
        first = bip340.sign(msg, self.VALID)
        second = bip340.sign(msg, self.VALID)
        self.assertTrue(bip340.verify(msg, pub, first))
        self.assertTrue(bip340.verify(msg, pub, second))
        # Fresh aux_rand per call, so two signatures over one message differ. Both
        # verify; BIP-340 signatures are not required to be deterministic.
        self.assertNotEqual(first, second)


class TestCurveInternals(unittest.TestCase):
    """A few facts the vectors rely on, asserted directly so a failure names itself."""

    def test_generator_is_on_the_curve(self):
        x, y = bip340.G
        self.assertEqual(pow(y, 2, bip340.P), (pow(x, 3, bip340.P) + 7) % bip340.P)

    def test_generator_has_order_n(self):
        self.assertIsNone(bip340._point_mul(bip340.G, bip340.N))

    def test_lift_x_rejects_off_curve_and_oversized(self):
        # Vector 5's pubkey is a valid field element that is not on the curve; 14's
        # exceeds the field size. Both must come back as None, for different reasons.
        self.assertIsNone(
            bip340._lift_x(bytes.fromhex("EEFDEA4CDB677750A420FEE807EACF21EB9898AE79B9768766E4FAA04A2D4A34"))
        )
        self.assertIsNone(
            bip340._lift_x(bytes.fromhex("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC30"))
        )

    def test_lift_x_always_returns_even_y(self):
        for v in VECTORS:
            point = bip340._lift_x(bytes.fromhex(v.pubkey))
            if point is not None:
                with self.subTest(vector=v.index):
                    self.assertEqual(point[1] % 2, 0)

    def test_precomputed_generator_chain_agrees_with_the_general_multiply(self):
        """_point_mul_g is a cache, so it must be indistinguishable from the real thing.

        The vectors already prove the fast path is right for the scalars they use; this
        covers the boundaries they do not — zero, one, a bit boundary, and the top of
        the group — so an off-by-one in the table cannot hide behind them.
        """
        for scalar in (0, 1, 2, 3, 255, 256, 257, 1 << 128, bip340.N - 1, bip340.N):
            with self.subTest(scalar=scalar):
                self.assertEqual(
                    bip340._point_mul_g(scalar), bip340._point_mul(bip340.G, scalar)
                )

    def test_the_generator_table_is_the_doubling_chain_it_claims(self):
        self.assertEqual(len(bip340._G_MULTIPLES), 256)
        self.assertEqual(bip340._G_MULTIPLES[0], bip340.G)
        for i in range(1, 8):
            with self.subTest(power=i):
                self.assertEqual(bip340._G_MULTIPLES[i], bip340._point_mul(bip340.G, 1 << i))

    def test_tagged_hash_is_domain_separated(self):
        self.assertNotEqual(
            bip340._tagged_hash("BIP0340/aux", b"x"),
            bip340._tagged_hash("BIP0340/nonce", b"x"),
        )


if __name__ == "__main__":
    unittest.main()
