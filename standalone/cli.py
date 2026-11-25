"""Command line entry point of the standalone analyst.

Examples:

  # talk with a local KoboldCpp model
  python -m standalone.cli -c mydata -p kobold

  # OpenRouter (or any other service preset), model required
  python -m standalone.cli -c mydata -p openrouter -m openai/gpt-4.1

  # Anthropic
  python -m standalone.cli -c mydata -p anthropic -k sk-ant-...

  # anything OpenAI-compatible, by URL
  python -m standalone.cli -c mydata -p http://127.0.0.1:5001/v1 -m my-model

Conversations persist in ./memory; pass -c <name> to resume one, or
``--list`` to show existing conversations.
"""

from __future__ import annotations

import argparse
import os
import sys

from .agent import chat, make_client
from .memory import Conversation, list_conversations


def build_parser() -> argparse.ArgumentParser:
    from .llm import OPENAI_COMPATIBLE_PRESETS

    parser = argparse.ArgumentParser(
        prog="standalone",
        description="Conversational data analyst: any model provider, local persistent memory.",
    )
    parser.add_argument("-c", "--conversation", default="default", help="Conversation name (memory key).")
    parser.add_argument("-p", "--provider", help="Preset name, or a full OpenAI-compatible base URL.")
    parser.add_argument("-m", "--model", help="Model name at the provider.")
    parser.add_argument("-b", "--base-url", help=f"Base URL; presets: {', '.join(sorted(OPENAI_COMPATIBLE_PRESETS))}.")
    parser.add_argument("-k", "--api-key", help="API key (otherwise the provider's environment variable).")
    parser.add_argument("--memory-dir", help=f"Where to keep memory (default {os.environ.get('ANALYST_MEMORY_DIR', './memory')}).")
    parser.add_argument("--skills-dir", help=f"Folder with skill files (default {os.environ.get('ANALYST_SKILLS_DIR', '<project>/skills')}).")
    parser.add_argument("--pc", action="store_true", help="Enable local PC control tools for this session (audit: pc_log.jsonl).")
    parser.add_argument("--list", action="store_true", help="List saved conversations and exit.")
    parser.add_argument("--list-models", action="store_true", help="List models available at the provider and exit.")
    parser.add_argument("-e", "--env-file", help="Optional .env file to load before anything else.")
    return parser


def _load_env_file(path: str) -> None:
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError as exc:
        print(f"Could not read {path}: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    import logging

    logging.getLogger().setLevel(logging.WARNING)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.env_file:
        _load_env_file(args.env_file)
    elif os.path.exists(".env"):
        _load_env_file(".env")

    if args.skills_dir:
        os.environ["ANALYST_SKILLS_DIR"] = args.skills_dir
    if args.pc:
        from .pc import set_enabled

        set_enabled(True)

    if args.list:
        saved = list_conversations(args.memory_dir)
        print("Saved conversations:", ", ".join(saved) if saved else "(none yet)")
        return 0

    if args.list_models:
        from .llm import list_models, resolve_api_key, resolve_base_url

        provider = args.provider or os.environ.get("ANALYST_PROVIDER", "")
        base_url = args.base_url or os.environ.get("ANALYST_LLM_BASE_URL") or None
        try:
            base = resolve_base_url(provider, base_url)
            if not base:
                print("Error: set the base URL (-b / ANALYST_LLM_BASE_URL) or a known provider.", file=sys.stderr)
                return 2
            models = list_models(base, resolve_api_key(provider, args.api_key), provider)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        print(f"Models at {base}:")
        for model_id in models:
            print(f"  {model_id}")
        return 0

    provider = args.provider or os.environ.get("ANALYST_PROVIDER", "")
    model = args.model or os.environ.get("ANALYST_MODEL")
    base_url = args.base_url or os.environ.get("ANALYST_LLM_BASE_URL") or None

    try:
        client = make_client(provider, model, base_url, args.api_key)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    from .skills import discover_skills

    skills = discover_skills()
    if skills:
        print("Skills available:", ", ".join(skill.name for skill in skills))

    root = args.memory_dir or os.environ.get("ANALYST_MEMORY_DIR") or None
    conv = Conversation(args.conversation, root).load()
    chat(conv, client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
