# Dispatch Preflight Internals (`live-settings`)

Developer specification extracted from runtime references explaining how `scripts/preflight.py` resolves Orca live agent settings (`live-settings`).

## Why Schema Probing Replaced Filename Rules

In Orca 1.4.265 (re-verified 2026-11-20; previously verified on 1.4.258 on 2026-11-13; originally observed on 1.4.244 on 2026-10-29), the profile directory resolved from `orca agent hooks status --json` (`result.settingsPath`) holds multiple SQLite files alongside backups (`*.backup.*.db`) — e.g. `orca-github-cache.db` and an empty `state.db`. Neither filename nor file count identifies the live settings database, and `orca-data.json` / backups must never be read as fallback.

## Read-Only SQLite Probe Contract

`preflight.py` opens every non-backup `*.db` in `dirname(result.settingsPath)` read-only (`?mode=ro`) and runs the literal query:

```sql
SELECT payload FROM profile_state_documents WHERE domain = ?
```

with parameter `'settings'`. Exactly **one** database must return a single JSON row containing non-empty string `defaultTuiAgent` and string array `disabledTuiAgents`. Zero or multiple usable candidates fail closed and name all candidates and rejection reasons.

Reference standalone snippet (automated inside `preflight.py check`):

```python
import json, pathlib, sqlite3

def probe_profile(profile_dir):
    usable = []
    for candidate in sorted(pathlib.Path(profile_dir).glob("*.db")):
        if ".backup." in candidate.name or not candidate.is_file():
            continue
        conn = sqlite3.connect(candidate.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT payload FROM profile_state_documents WHERE domain = ?",
                ("settings",),
            ).fetchall()
        finally:
            conn.close()
        if len(rows) == 1:
            doc = json.loads(rows[0][0])
            if isinstance(doc.get("defaultTuiAgent"), str) and isinstance(doc.get("disabledTuiAgents"), list):
                usable.append((candidate.name, doc["defaultTuiAgent"], doc["disabledTuiAgents"]))
    return usable
```
