## Summary

<!-- 1-3 bullets on what changed and why. -->

## Preview

<!--
For user-facing changes (Discord embeds, slash command shape, persona copy,
voice library additions, etc.), include an ASCII/markdown mock of the new
surface so reviewers don't have to deploy to see it.

For UI:
```
┌─ embed: "title" ────────────────────────────────┐
│ description...                                  │
│ ▾ select                                        │
│ [button]                                        │
└──────────────────────────────────────────────────┘
```

For copy / persona / voice changes, quote 2-3 sample outputs:
> "input"  →  "Toots' response"

Delete this section if the change is backend-only (db schema, refactor,
dep bump), don't pad with "N/A".
-->

## Test plan

- [ ] `ruff check .`
- [ ] `mypy .`
- [ ] `pytest`
- [ ] Verified on Railway deploy in `#bot-logs` (if applicable)
