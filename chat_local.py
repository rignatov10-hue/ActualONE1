"""Interactive terminal chat with the analyst agent, keeping one session alive.

Works both with the default Mistral model and, when ANALYST_LLM_BASE_URL is set,
with any OpenAI-compatible local server (KoboldCpp, llama.cpp server, Ollama's
OpenAI shim, LM Studio, ...). Requires MISTRAL_API_KEY in any case: the Google
Drive connector calls it even when the model is local.
"""

from __future__ import annotations

import asyncio

from main import agent

EXIT_WORDS = {"exit", "quit", "выход"}


async def chat() -> None:
    session = agent.start_session()
    session_state = None
    print("Чат с аналитиком. Пришлите ссылку на CSV из Google Drive и задавайте вопросы.")
    print("Пустая строка или 'exit' — выход.")
    while True:
        try:
            message = input("\nВы: ")
        except (EOFError, KeyboardInterrupt):
            break
        if not message.strip() or message.strip().lower() in EXIT_WORDS:
            break
        if session_state is None:
            session_state = session
        turn = await session.run(message)
        print(f"\nАгент: {turn.text}")


if __name__ == "__main__":
    asyncio.run(chat())
