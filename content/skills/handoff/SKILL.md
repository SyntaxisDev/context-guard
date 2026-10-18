---
name: handoff
description: Write or resume a chat handoff so work continues in a fresh, small chat. Use when the user says "handoff", "wrap up", "new chat", "continue later", when a context-guard hook asks for it, or when resuming from a previous chat or its transcript.
---

# Handoff

A handoff is a short file that lets a new chat, in {{runtime_name}} or any other runtime, continue the work without re-reading the old one. It carries the findings and decisions, not the history.

## Rules

{{handoff_rules}}

## Writing a handoff

1. Path: use the hook-provided path or keep the existing task file, following the rules above. Example name: `handoff-task-naming-<suffix>.md` for a task about improving handoff filenames.
2. Write from what is already in your context. Don't re-read files or re-run commands just for the handoff.
3. Keep it under ~40 lines, using this template:

```markdown
# <task title>

Status: open · Updated: <date time> · Source runtime: {{runtime_name}} · Source chat: <chat name or id>
Workspace: <absolute workspace root; for worktree tasks the worktree path>

## Goal

<one or two sentences: what "done" means>

## Where things are

- Repo / worktree / branch / PR: <paths and links>
- Key files: <path — why it matters>

## Decisions (and why)

- <decision> — <reason>; rejected: <option> because <reason>

## Done

- <verified facts and completed steps, with commit SHAs where relevant>

## Open / next steps

1. <next concrete step>

## Commands that work

- `<command>` — <what it does / where to run it>

## Don't

- <things not to redo, re-review, or touch>
```

4. Save it with a file-writing tool in the same turn it was asked for; never say a handoff is updated unless you wrote it in this turn. If a `[context-guard]` bookkeeping message or a context note asked for the handoff, write the file and do not mention it. If the user asked for it, reply with one short line such as "Handoff saved; a new chat will pick it up." Do not name the path unless the user asks for it.

## Resuming from a handoff

- A new chat gets a ranked list of open handoffs at session start, with the likely match marked. If no list was delivered, list `{{handoff_dir}}` newest first and search it by keyword.
- A handoff marked "(likely match)" is the default: read it unless the request is clearly unrelated. Otherwise, if the request matches one, confirm the match in one line and read it; if two could match, ask which.
- Read the handoff first and trust "Done" and "Decisions" unless the code contradicts them; if it does, say so before acting.
- Re-read only the files needed for the next step.
- Keep updating the same file. When the task is finished, set `Status: done`.

## Resuming a chat that has no handoff

Use this when an old chat went cold and has no handoff. Do it from a NEW chat; reopening the old chat re-reads its whole context.

1. Find the transcript: {{transcripts}} Search by chat ID or a distinctive phrase.
2. Extract only the user and assistant text with a script (skip tool payloads). It is usually 20-50k tokens, compared with 300-600k of live context.
3. Write a handoff from it using the template above, then continue from the handoff.
