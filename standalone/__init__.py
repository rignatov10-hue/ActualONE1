"""Standalone conversational data analyst: no Mistral dependency at all.

- Model: any OpenAI-compatible endpoint (KoboldCpp, Ollama, OpenAI, OpenRouter,
  DeepSeek, Groq, vLLM, ...) or Anthropic, chosen by env/CLI.
- Data: Google Drive links (public, or private with your own Google OAuth token),
  direct https URLs to CSV files, or local files.
- Memory: everything lives on disk under ``memory/`` - conversation history in
  JSONL and the loaded dataset as parquet - so memory survives restarts and is
  independent of any AI provider.
"""
