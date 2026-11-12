# Recording onboarding context

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Record to memory

User preferences go into **shared memory** (`meiva.memory.SHARED_PATH`), never only
personal memory — every agent must see them. Write with absolute paths: a
relative path resolves against `meiva.cwd`, not your workspace, and the note
lands in the wrong directory. Search first and update an existing canonical
note instead of adding a duplicate.

A pool note is plain Markdown: a title, one short paragraph stating the rule or
fact, then the body. There is no required frontmatter, tag or attribution
comment; consolidation adds `description` and `verified` later. Keep a note on
one topic, under 8000 characters, with explicit `YYYY-MM-DD` dates:

```markdown
# <short title>

<One paragraph: the rule or fact.>

<Body: details, Why, How to apply.>
```

### Which note takes what

| You learned | Kind | File | Example |
|---|---|---|---|
| Who the user is — name, contact, language, timezone, accounts | user | `<pool>/user-profile.md` (one consolidated note) | "User is on Beijing time" |
| How to work with them — channels, gates, cadence, corrections | feedback | one note per rule or per cluster of related rules | "Serve pages, never email reports" |
| An ongoing goal and its constraints | project | `<pool>/projects/<slug>.md` | "Track competitor X, weekly" |
| A role an agent was given and its boundary | role | `<pool>/agents/<name>.md` | "Health steward: owns health domain only" |
| A machine or cluster fact discovered | env | `<pool>/infra/...` | "Backups run at 3 AM" |
| A pointer to an external resource | reference | `<pool>/...` | "Their Notion workspace URL" |

The kind is a way of choosing the file, not a field to write. Add the new note
to the pool's `MEMORY.md` index when it is a standing fact every agent needs.

A feedback note leads with the rule, then the reason, then how to apply it:

```markdown
# <the rule as a title>

<the rule>

Why: <what the user said, or what broke when this was ignored>
How to apply: <when it fires and what to do>
```

Keep agent-private workflow state (your own checklist, drafts) in your
personal memory (`meiva.memory.PERSONAL_PATH`) instead — shared memory is for facts other agents need.
