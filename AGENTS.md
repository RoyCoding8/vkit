# Contribution workflow

Integrate reviewed and verified changes directly into `main`. Do not open pull requests unless the user
explicitly asks for one.

When delegation is authorized, use Luna agents in isolated worktrees with disjoint file ownership.
The coordinating agent reviews and merges changes one at a time, verifies the integrated result, and
removes every worker worktree and merged worker branch before handing back.

Subtract before adding. Reuse existing mechanisms, remove obsolete code, and consolidate overlapping
operations when one precise function can replace them.

# Agent tool discovery

When vkit is connected through MCP, call its `help` tool to discover currently enabled tools and their
input schemas. Pass `query` to search for a question you need answered. Use each result within its stated
scope. Do not maintain a static tool inventory here.
