# drive-data-analyst

A conversational data analyst agent. Give it a CSV file and ask questions in
plain language; it answers with real numbers from your data, remembers earlier
turns, and never runs arbitrary code against the data (five fixed pandas
tools - that is all).

Two ways to run it:

- **`standalone/` - fully autonomous, no Mistral anywhere.** The model is any
  OpenAI-compatible service (KoboldCpp, Ollama, LM Studio, OpenAI, OpenRouter,
  DeepSeek, Groq, vLLM...) or Anthropic; Google Drive is read directly from
  Google (public files at once, private ones through your own Google OAuth
  token); memory is local files you own. Copy this folder to your PC and it
  works. See [Standalone](#standalone-no-mistral-required).
- **`main.py` - the Mistral Agents SDK version** for a managed deployment on
  Mistral Cloud (needs the Mistral connector + a paid plan). See
  [Model providers](#model-providers) and [Deploy](#deploy-mistral-managed).

## Быстрый старт по-русски (используем GUI / EXE)

1. **Скачать проект.** На странице репозитория нажмите зелёную кнопку **Code →
   Download ZIP**, распакуйте архив в любую папку.
2. **Установить Python** с https://www.python.org/downloads/ (при установке
   поставьте галочку **Add python.exe to PATH**). Уже установлен — пропустите.
3. **Собрать EXE.** Дважды щёлкните `make_exe.bat` и подождите несколько минут:
   он сам поставит зависимости и соберёт `dist\Analyst.exe`. Python после этого
   больше не нужен — `Analyst.exe` можно скопировать куда угодно.
4. **Запустить модель.** Откройте KoboldCpp со своей GGUF-моделью (например
   Qwen3.5-9B), включите OpenAI-compatible API, порт **5001**, и дождитесь
   загрузки модели.
5. **Настроить агент.** Откройте `Analyst.exe`:
   - Провайдер: `kobold`
   - Модель: любое имя (Кобольт его игнорирует), например `qwen3.5-9b`
   - API-ключ: оставьте пустым (Кобольт локальный, ключ не нужен)
   - Адрес (URL): можно не заполнять — подставится сам
6. Нажмите **«Запустить чат»**, вставьте ссылку на CSV/таблицу из Google Drive:
   `загрузи https://drive.google.com/... и опиши колонки` — и задавайте вопросы.
   Публичные файлы Drive ("доступен всем, у кого есть ссылка") работают сразу;
   приватные — после однократной привязки Google (кнопка «Подключить Google» в
   настройках, шаги — ниже в разделе про приватные файлы).

Настройки хранятся в `gui_config.json`, память диалогов — в `memory\` рядом с
exe. Скопируйте их вместе с exe — перенесёте и разговор, и загруженные данные.

Вместо EXE можно запускать без сборки: `python gui_entry.py` из папки проекта
(нужен `pip install pandas pyarrow openpyxl`).

## Standalone (no Mistral required)

Everything lives in the project folder, so the whole directory is portable:

```
standalone/      the agent (tools, memory, LLM clients, CLI)
memory/          created at first run: conversations + loaded datasets
.env             your model provider settings
```

### 1. Install (any PC, Python 3.10+)

```bash
uv sync                      # or: pip install pandas pyarrow openpyxl
```

### 2. Pick the model with .env (one provider at a time)

```bash
# a) local model in KoboldCpp (http://127.0.0.1:5001/v1)
ANALYST_PROVIDER=kobold
ANALYST_MODEL=my-model                 # any name; Kobold ignores it
OPENAI_API_KEY=local

# b) a paid service, e.g. OpenRouter:
# ANALYST_PROVIDER=openrouter
# ANALYST_MODEL=openai/gpt-4.1
# OPENAI_API_KEY=sk-or-...    (or deepseek/groq/openai/anthropic presets)
```

The model must support tool/function calling (Qwen3 chat templates and modern
GLM models do).

### 3. Run

```bash
uv run python -m standalone.cli -c mydata -p kobold
# or, without .env, fully explicit:
uv run python -m standalone.cli -c mydata \
  -p http://127.0.0.1:5001/v1 -m my-model
uv run python -m standalone.cli --list      # saved conversations
```

Flags: `-c` conversation name (its own memory), `-p` provider preset or URL,
`-m` model, `-k` API key, `--memory-dir` memory location. Presets: `kobold`,
`ollama`, `lmstudio`, `vllm`, `openai`, `openrouter`, `deepseek`, `groq`,
`together`, `mistral-api`, `anthropic`, or any OpenAI-compatible URL.

Then just chat: paste a Google Drive link (or an https URL to a CSV, or a local
path) and ask questions. Follow-ups remember the file and everything discussed.

### Private Google Drive files (one-time setup)

Public ('anyone with the link') files work out of the box. For private files
link your own Google account once - Google is accessed directly, with no
intermediary service:

1. https://console.cloud.google.com - create a project, enable the
   *Google Drive API*;
2. *OAuth consent screen*: External + yourself as test user;
3. *Credentials* - create a *Desktop app* OAuth client, download
   `credentials.json` into the project folder;
4. `uv run python -m standalone.oauth_setup` - a browser opens, allow
   read-only Drive access. `google_token.json` appears and renews itself.

Both files are git-ignored; delete them and the access is gone.

### Memory

Memory is plain local files - independent of any AI provider:

```
memory/conversations/<name>.jsonl   every conversation turn
memory/datasets/<name>.parquet      the loaded dataset + narrowed scope
```

Stop the program any time; next run with the same `-c <name>` resumes the
discussion and the loaded dataset exactly where you left it.

### Desktop GUI (double-click, and no Python afterwards)

`gui_entry.py` opens a window: enter the provider, model and API key, press
**Start chat**, and the chat opens - same agent, same tools, same memory
(`gui_config.json` and `memory\` are created next to the program).

Run it in place:

```bash
python gui_entry.py
```

### Build the Windows EXE (once, on a Windows PC)

A Windows EXE can only be built on Windows (PyInstaller does not cross-build).
One double-click does everything:

1. Install Python from python.org (check **Add python.exe to PATH**).
2. Double-click `make_exe.bat` and wait; it installs dependencies and builds
   `dist\Analyst.exe`.
3. From then on: copy `Analyst.exe` anywhere (another PC too), double-click it,
   fill the settings once, chat. Python is no longer needed on that machine.

The EXE is a single file; settings live in `gui_config.json` and memory in
`memory\` next to it - copy these along to move the conversations too.

The model must be reachable from the machine the EXE runs on: a local KoboldCpp
server on the same PC (`-p kobold` style URL `http://127.0.0.1:5001/v1`) or any
cloud provider by URL and key.

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
