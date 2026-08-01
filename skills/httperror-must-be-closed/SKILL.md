---
name: httperror-must-be-closed
description: Use when a test suite emits "ResourceWarning: Implicitly cleaning up <HTTPError 403: 'Forbidden'>" from tempfile or another unrelated module.
---

# An HTTPError holds an open socket, and reading it is not closing it

**Failed:** a suite exercising 4xx paths →
`tempfile.py:484: ResourceWarning: Implicitly cleaning up <HTTPError 403: 'Forbidden'>`

**Why:** `urllib.error.HTTPError` is itself a response object with a live connection;
catching it and calling `.read()` leaves the descriptor open, and the leak is reported
later from whichever module happens to trigger garbage collection.

**Fix:** close it in a `finally`, so a failed read still releases the socket.

```python
except urllib.error.HTTPError as exc:
    raw = b""
    try:
        raw = exc.read(MAX_BYTES)
    finally:
        exc.close()
    return Reply(exc.code, _decode(raw))
```

**Verify:** `python3 -W error::ResourceWarning -m unittest <suite>` — passes instead of erroring.
