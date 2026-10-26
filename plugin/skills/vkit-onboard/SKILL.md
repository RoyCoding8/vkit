---
name: vkit-onboard
description: Set up vkit verification for a repository. Use when the user asks to onboard a project, add verification checks, or write a feature map, or when status reports no manifest.
---

# Onboard a repository

## Discover the interface

| Action | Call |
| --- | --- |
| Find enabled tools | `help`. |
| Read a tool's contract | `help(name=...)` before use. |
| Submit in compact mode | `invoke(name="propose", arguments={...})`. |

## Onboarding sequence

1. Run `vkit project inspect --project <root>`.
2. Review the declared test commands and their source file and line. Report ambiguity rather than choosing silently.
3. Draft checks and any supported formal components using the installed version-3 manifest schema.
4. Draft the feature map.
5. Submit both through `propose`, including any new driver files.
6. Stop and ask the user to review with `vkit proposals` and apply with `vkit accept --proposal <digest>`.

## Check and feature requirements

| Artifact | Required content |
| --- | --- |
| Check | Prefer a scenario driver that runs the real application. |
| Formal component | Bind supported source APIs to the installed package's fixed contracts. Keep model assumptions and coverage explicit. |
| Scenario report | Scenario ID, PASS or FAIL, and the observed result. |
| `inputs` | Files read by the check. Unrelated edits should not make its evidence stale. |
| Feature entry | User-visible behavior, how the user reaches it, entry points, covering checks, and unverified gaps. |
| Proposal | Checks, feature entries, and any new driver files. |

## Acceptance boundary

| State | Meaning |
| --- | --- |
| Proposed | The proposed definitions and files do not exist or run until accepted. |
| Acceptance | Requires the user's CLI action. No MCP tool accepts proposals. |

## Rules

- Derive expected results from the user or product specification. A driver that asserts the current implementation's behavior establishes nothing beyond that behavior.
- Never install dependencies. Report a missing prerequisite and stop.
- A feature without a check is unverified. Do not describe it as working.
