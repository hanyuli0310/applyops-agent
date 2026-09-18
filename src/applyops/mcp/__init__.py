"""MCP surface for ApplyOps.

This package is the *hands and memory* layer: browser primitives, form actions
that carry their own flywheel bookkeeping, the memory store, and the safety
rails. It deliberately contains no model calls and no agent loop -- the calling
harness supplies the judgement, and this server supplies the ability to act and
the ability to remember.

That split is what makes the server harness-agnostic: WorkBuddy, Codex, Claude
Code or a plain script all drive the same tools over stdio.

Entry point: ``applyops-mcp`` (see ``server.main``).
"""

__all__ = ["server"]
