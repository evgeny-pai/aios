---
name: urllib-ignores-userinfo
description: Use when a URL with a token in it fails with "nodename nor servname provided, or not known" or "Name or service not known" — or when a credential shows up in a log next to that error. urllib.request does not implement userinfo; wget and curl do.
---

# `http://user:pass@host/` is not a URL urllib can fetch

**Failed:** a test fed `forge binpkg --peer` a peer URL shaped exactly the way portage
authenticates against a binhost, against a server on loopback:

```
forge: http://x:s3cr3t@127.0.0.1:49490/binpkgs/Packages:
  <urlopen error [Errno 8] nodename nor servname provided, or not known>
```

Nothing was down. Nothing was misspelled.

**Why:** `Request._parse` splits the scheme and the host and stops. There is no
userinfo handling anywhere in `urllib.request`, so `http.client` receives
`x:s3cr3t@127.0.0.1:49490` *as the host*, splits the last colon off as the port, and
resolves the rest. Hence a DNS error for a literal IP address. `wget` and `curl` do
implement it, which is why `portage.binhost_env` puts the token in the URL at all —
the two fetchers disagree, and the code that hands a URL to both cannot assume either.

**Fix:** authenticate explicitly, and never let the URL carry the secret into a message.
`forge.binpkg._credential` does exactly this:

```python
parts = urllib.parse.urlsplit(url)
if parts.username:
    user, password = map(urllib.parse.unquote, (parts.username, parts.password or ""))
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    request.add_unredirected_header("Authorization", f"Basic {auth}")
    url = parts._replace(netloc=parts.netloc.rpartition("@")[2]).geturl()
```

`unquote` because wget and curl decode the userinfo before spending it; skip it and
the two fetchers authenticate as different users against one URL. `rpartition`
because a password may contain `@`.

Two things that bite after it works:

- **`add_unredirected_header` is dropped on every 3xx** — that is what "unredirected"
  means, and it is the right default. A binhost that 302s `/binpkgs` to `/binpkgs/`
  then answers 401. Re-attach it *after* an origin check, never before:
  `_PinnedRedirect.redirect_request` compares `_origin(newurl)` to
  `_origin(req.full_url)` (both userinfo-blind, since `urlsplit().hostname` strips it)
  and only then puts the header back.
- **Keep dialing and reporting separate.** Every error message is prefixed with the
  URL, so the whole URL goes to `_shown`/`forge.portage.redact_url` and only the
  stripped one reaches `Request`. The DNS failure above put a live token on the
  terminal, which is the worse half of this bug.

**Verify:** `python3 -m unittest tests.test_binpkg.TestPeerCredential
tests.test_binpkg.TestPeerCredentialRedirect` — the fixture's binhost answers 401 to
any request without the header, so every read that succeeds is proof the token
reached the wire, and every test also asserts it reached neither stdout nor stderr.
