---
name: session-investigation
description: Run a bounded investigation that summarizes a loaded agent session, its objective, approach, outcomes, and lessons.
---

Choose `investigate` only when the current user explicitly asks to investigate, summarize, or analyze the loaded
session as a workflow. Questions that can be answered directly from current evidence should use `answer` instead.

The investigation is read-only. It starts from the current dashboard selection and manages its own normalized-event
searches and exact turn reads instead of receiving a host-selected representative timeline.
