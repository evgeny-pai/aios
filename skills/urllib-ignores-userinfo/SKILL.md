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

```python
parts = urllib.parse.urlsplit(url)
if parts.username:
    auth = base64.b64encode(f"{parts.username}:{parts.password or ''}".encode()).decode()
    request.add_unredirected_header("Authorization", f"Basic {auth}")
    url = parts._replace(netloc=parts.netloc.rpartition("@")[2]).geturl()
```

Every error message built from that URL is prefixed with it, so redact before it is
raised — `forge.portage.redact_url`. The DNS failure above put a live token on the
terminal, which is the worse half of this bug.

**Verify:** `python3 -m unittest tests.test_binpkg.TestPeerCredential` — it dials a real
server with a userinfo URL and asserts the token reaches neither stdout nor stderr.
