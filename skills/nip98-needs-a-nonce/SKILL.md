---
name: nip98-needs-a-nonce
description: Use when a Nostr relay rejects a legitimate request with "NIP-98: replay detected" or a 401 on the second of two identical requests.
---

# Two identical requests in one second are the same NIP-98 event

**Failed:** a polling responder issuing the same `POST /query` twice →
`401 NIP-98: replay detected`

**Why:** the auth event is fully determined by (method, url, body, `created_at`), and
`created_at` counts **seconds** — so two identical requests inside one second hash to the
same event id, and the relay's replay guard refuses the second.

A fresh signature is not enough: BIP-340 signing takes random `aux_rand`, so two such
events differ in `sig` while sharing an `id`, and the id is what the guard records.

**Fix:** add a nonce tag; NIP-98 ignores unknown tags, and the id changes.

```python
tags = [["u", url], ["method", method.upper()], ["nonce", os.urandom(16).hex()]]
```

**Verify:** assert on IDS, not header strings, at a fixed timestamp —
`len({id_of(auth_header(..., created_at=1700000000)) for _ in range(25)}) == 25`.
A test comparing the header strings passes while the bug is live.
