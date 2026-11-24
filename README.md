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
uv run --env-file .env python chat_local.py   # interactive chat, one session
uv run pytest                # run the tool tests (no key needed)
```

## Model providers

The same agent can run on Mistral, a local model server, or third-party AI
services. Choose with `ANALYST_PROVIDER` in `.env` and set the matching key:

| `ANALYST_PROVIDER` | Model source | Key | Extra variable |
| --- | --- | --- | --- |
| `mistral` (default) | Mistral Cloud | `MISTRAL_API_KEY` | — |
| `openai-compatible` | KoboldCpp, llama.cpp, LM Studio, OpenRouter, DeepSeek, vLLM | `OPENAI_API_KEY` (or the service's key under this name) | `ANALYST_LLM_BASE_URL` |
| `openai` | OpenAI | `OPENAI_API_KEY` | — |
| `anthropic` | Anthropic | `ANTHROPIC_API_KEY` | — |

`ANALYST_MODEL` names the model at that provider (defaults: `mistral-medium-latest`,
`gpt-4.1`, `claude-sonnet-4-5`, `local-model`). `ANALYST_LLM_BASE_URL` can also
override the default endpoint of `mistral`/`openai`/`anthropic`.

A model behind `openai-compatible` must support OpenAI-style tool/function
calling (Qwen3 chat templates and recent llama.cpp builds do).

`MISTRAL_API_KEY` is required in every mode: the Google Drive connector calls it
even when the model itself is local or third-party.

### Local model example (KoboldCpp)

1. Start KoboldCpp with your GGUF model; it serves `http://127.0.0.1:5001/v1`.
2. Put this in `.env`:

```bash
MISTRAL_API_KEY=...            # for the Google Drive connector
ANALYST_PROVIDER=openai-compatible
ANALYST_LLM_BASE_URL=http://127.0.0.1:5001/v1
ANALYST_MODEL=my-local-model   # any name; the server usually ignores it
OPENAI_API_KEY=local           # any non-empty value
```

3. Run `uv run --env-file .env python chat_local.py`.

Without `ANALYST_PROVIDER`/`ANALYST_LLM_BASE_URL` the agent uses Mistral Cloud
and `agents deploy` works as below.

> A managed deployment on Mistral Cloud cannot reach a model on your machine:
> the local-model mode is for `chat_local.py` / `agents serve` runs only.

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
