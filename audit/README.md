# audit/ — proposed-fixes log for read-only scheduled tasks

Cowork scheduled tasks (e.g. `fno-scan-cycle`, `scanner-vs-chartink-daily-comparison`)
run **read-only on this repo's code**. They must never edit source files, never run
`git add`, and never commit.

When such a task observes a probable bug while doing its normal work, it appends a
single structured line to **`proposed_fixes.jsonl`** and exits. The operator reviews
the log and decides whether to act. This keeps an audit trail and prevents automated
sessions from silently mutating trading code.

## Schema

One JSON object per line (JSON Lines). Fields:

| Field          | Type   | Description |
| -------------- | ------ | ----------- |
| `timestamp`    | string | ISO 8601 with timezone, e.g. `2026-06-04T15:47:12+05:30` |
| `session_id`   | string | The scheduled-task session id (for tracing back to the run) |
| `task_name`    | string | e.g. `fno-scan-cycle`, `scanner-vs-chartink-daily-comparison` |
| `observation`  | string | What the task observed (the symptom) |
| `file`         | string | `path/to/file:line` if known, else best guess or `""` |
| `suggested_fix`| string | Short description of the proposed fix |

### Example

```json
{"timestamp": "2026-06-04T15:47:12+05:30", "session_id": "abc123", "task_name": "fno-scan-cycle", "observation": "preflight endpoint returned 500 on every cycle today", "file": "blueprints/preflight.py:42", "suggested_fix": "guard against missing daily_intent row before dereferencing"}
```

## Rules

- **Append-only.** Never rewrite or delete existing lines.
- **No code edits.** Tasks observe and report; the operator fixes.
- The first line of `proposed_fixes.jsonl` is a `_comment` marker, not a fix entry —
  skip it when parsing.
