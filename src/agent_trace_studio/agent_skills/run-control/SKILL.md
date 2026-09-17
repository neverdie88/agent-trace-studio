---
name: run-control
description: Continue, restart, discard, or stop the current Agent Trace Studio workflow when the user explicitly requests it.
---

Choose `continue_run`, `restart_run`, `discard_run`, or `cancel_run` only for an explicit command about the current
workflow. Use `answer` when the user merely asks what an action would do or why a run stopped.

The backend validates that the requested action is available for the current run.
