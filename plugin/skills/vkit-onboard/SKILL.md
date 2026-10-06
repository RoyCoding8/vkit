---
name: vkit-onboard
description: Set up vkit verification for a repository. Use when the user asks to onboard a project, add verification checks, or write a feature map, or when status reports no manifest.
---

# Onboard a repository

## Order

1. Run `vkit project inspect --project <root>`. It lists the test commands the repository already declares,
   with the file and line each came from. Report ambiguous ones instead of picking.
2. Draft checks in `verification/manifest.json` (schema_version 2) with the user. Prefer a scenario driver
   that runs the real application and writes each scenario's id, PASS or FAIL, and what it observed. Declare
   `inputs` as the files the check reads, so unrelated edits do not make it stale.
3. Draft `verification/features.json`: each user-visible behavior, its entry point, the setup to reach it,
   which checks cover it, and the gaps nobody has checked.
4. Stop and ask the user to review and run `vkit accept --project <root>`. Nothing you drafted can run until
   they do, and no tool lets you accept it.

## Rules

- The expected results come from the user or the product's specification, not from reading the current code.
  A driver that asserts whatever the code does today proves nothing.
- Never install dependencies. Report a missing prerequisite and stop.
- A feature with no check is unverified. Say so; do not describe it as working.
