# drive-data-analyst

A conversational data analyst agent built with the Mistral Agents SDK
(`mistralai-agents` 1.1). Give it a link to a CSV file in your Google Drive and
ask questions in plain language; it answers with real numbers from your data.

## Design

- **Google Drive via connector**: the agent mounts the `google_drive_mcp`
  Mistral connector. In a deployed session it runs on behalf of the user who is
  chatting, so files are fetched with *their* Google Drive connection; no
  credentials are stored in this project.
- **Fixed tools only, no arbitrary code execution**: the agent has no sandbox,
  no shell and no file tools. Its only capabilities are five `@agents.tool`
  functions implemented with pandas:
  - `load_csv_from_drive` — parses a Drive link/file ID, downloads the file
    through the connector gateway as the session user (Google Sheets are
    exported as CSV), parses it, and keeps it per session (with a disk cache at
    `/tmp/drive_analyst`). Returns shape and column names/types only.
  - `describe_columns` — dtype, nulls, distinct values, numeric summary stats
    or most frequent values per column.
  - `filter_rows` — fixed comparison vocabulary (`eq`, `ne`, `gt`, `gte`, `lt`,
    `lte`, `between`, `contains`, `starts_with`, `ends_with`, `in`, `not_in`,
    `is_null`, `not_null`); returns the match count plus a bounded sample; can
    narrow the scope for later questions with `set_as_active`.
  - `group_by_aggregate` — group by one or more columns and compute
    `count`/`sum`/`mean`/`median`/`min`/`max`/`std`/`nunique` per selected
    metric column, with ordering and a bounded result table.
  - `reset_scope` — restore the full dataset after narrowing.
- **Conversation memory**: sessions are durable; the loaded file, any narrowed
  scope, and the whole discussion persist across follow-ups in the same
  conversation. A new `load_csv_from_drive` replaces the previous file.
- **The model never sees the raw file**: the CSV is downloaded inside the load
  tool; tool answers carry only schema, statistics, and bounded samples.

## Files

- `main.py` — the agent, its instructions, and the fixed tools.
- `tests/test_tools.py` — pandas-level tests of the fixed tools (no model, no
  network).

## Local development

```bash
uv sync                      # install dependencies
cp .env.example .env         # then paste a Mistral API key into .env
uv run --env-file .env agents chat     # chat locally
uv run pytest                # run the tool tests (no key needed)
```

## Deploy (mistral-managed)

`agents deploy` registers the agent as a Workflows managed deployment. Because
the agent acts on behalf of the session user (connector with default
`run_as`), the deployment is created hardened.

```bash
git push
uv run agents deploy
```

Requires: a GitHub repository holding this project, the GitHub connector
connected in Mistral Studio, and a `MISTRAL_API_KEY` of a user with Workflows
access. `.env` is git-ignored and never leaves your machine.
