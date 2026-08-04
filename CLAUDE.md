# CLAUDE.md

Project-specific instructions for Claude Code sessions working in this repo.

## Workflow preference: propose, don't apply

Do not edit files directly. Instead, present proposed changes as diffs or
code blocks (with a short explanation of what and why), and let the user
review and apply them manually. This applies to bug fixes, refactors, new
code, and config changes alike — not just large/risky changes.

Exceptions (fine to apply directly unless told otherwise):
- Files you were explicitly asked to create/edit yourself (e.g. this file).
- Non-code artifacts like this CLAUDE.md, TODO.md updates, or docs, when the
user has clearly asked for the file to be written rather than just discussed.
