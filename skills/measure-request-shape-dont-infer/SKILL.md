---
name: measure-request-shape-dont-infer
description: Use when a server rejects a request body with a type complaint like "invalid type: map, expected a sequence" and the field names all look correct.
---

# The wrapper was wrong, not the contents

**Failed:** `POST /query` with `{"filters": [{"kinds": [0]}]}` — the shape a REST API
usually takes → `400 {"error":"invalid filters: invalid type: map, expected a sequence at
line 1 column 0"}`

**Why:** the endpoint takes a BARE ARRAY of filters; the error names the outer type, so it
reads like a complaint about the filter rather than about the envelope around it.

**Fix:** send the array, and pin the shape with a test that inspects what left the client.

```python
body = json.dumps(filters if isinstance(filters, list) else [filters]).encode()
```
```python
sent = json.loads(calls[0]["raw"])
self.assertIsInstance(sent, list)   # a dict here is the bug, and it is silent
```

**Verify:** try all three shapes against the real server and keep the one that returns 200 —
`{"filters":[…]}` and a bare object both 400 where `[{…}]` succeeds.
