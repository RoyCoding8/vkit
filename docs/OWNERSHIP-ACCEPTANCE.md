# Ownership acceptance contract

This is the exercised ownership contract for the existing app, extracted
verbatim from the completed Plan 02 at the e211eb2 implementation baseline.
It is reference material, not an implementation handoff. The runnable driver
is `scripts/acceptance02.py`; its contract tests are `tests/test_acceptance02.py`.

| Exercise | Required outcome |
| --- | --- |
| 100 competing claim attempts across multiple OS processes | Exactly one owner for an exclusive resource; explicit conflicts for the rest |
| Two disjoint resource sets | Both owners can progress |
| Multi-resource conflict | No partial claim survives the failed acquisition |
| Transaction owner killed | Database remains usable and no partial accepted claim appears |
| Start retried before/after client disconnect | One execution and the same run identity |
| MCP-like parent process exits | Supervisor continues or reports a documented blocked launch; no fictitious success |
| Cancel repeated or races with completion | One coherent terminal outcome; no unrelated process killed |
| Supervisor dies | Descendants handled within the tested containment boundary; uncertain claims stay reserved |
| Old owner submits after supersession | Rejected even if its previous check passed |
| Changed contract or policy | Old runs cannot satisfy the new requirements automatically |
| All checks pass but one required check is absent | BLOCKED |
| Client requests fewer checks than approved policy requires | Mandatory baseline remains required; no READY from the smaller selection |
| Disk/locking failure | No fabricated READY; state and artifacts remain diagnosable |
| Stateful operation sequences | No duplicate ownership or stale-attempt acceptance |
