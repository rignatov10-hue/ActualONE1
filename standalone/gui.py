"""Desktop GUI for the standalone analyst: settings for the model provider and
key, then a chat window. Standard tkinter, no new dependencies.

Run without any window: ``python gui_entry.py``. Built as a Windows EXE with
``make_exe.bat`` (PyInstaller); the EXE keeps its config and memory in the
folder it is placed in, so it is fully portable.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from pathlib import Path
from tkinter import (
    BOTH,
    DISABLED,
    END,
    LEFT,
    NORMAL,
    StringVar,
    Tk,
    W,
    X,
    messagebox,
    ttk,
)

CONFIG_NAME = "gui_config.json"
GOOGLE_OAUTH_SETUP_STEPS = """Приватные файлы Google Drive (заполнить один раз):

1. https://console.cloud.google.com - создайте проект (бесплатно)
2. API & Services - Library - включите Google Drive API
3. OAuth consent screen - External - добавьте свой email как Test user
4. Credentials - Create credentials - OAuth client ID - тип Desktop app -
   скачайте JSON как credentials.json в папку с программой
5. Нажмите здесь 'Подключить Google' и войдите в браузере

Токен сохранится в google_token.json и будет обновляться сам.
Общий доступ к файлам ('все, у кого есть ссылка') работает без этой настройки."""


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def config_path() -> Path:
    return app_dir() / CONFIG_NAME


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config: dict) -> None:
    try:
        config_path().write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        messagebox.showwarning("Аналитик", f"Не удалось сохранить настройки: {exc}")


class SettingsFrame(ttk.Frame):
    FIELDS = (
        ("provider", "Провайдер", "kobold"),
        ("base_url", "Адрес (URL)", ""),
        ("model", "Модель", ""),
        ("api_key", "API-ключ", ""),
        ("conversation", "Имя диалога (память)", "mydata"),
    )

    PRESET_HINTS = {
        "kobold": "KoboldCpp: http://127.0.0.1:5001/v1 (обычно вставлять не нужно)",
        "ollama": "Ollama: http://127.0.0.1:11434/v1",
        "lmstudio": "LM Studio: http://127.0.0.1:1234/v1",
        "openai": "api.openai.com/v1 + ключ sk-...",
        "openrouter": "openrouter.ai/api/v1 + ключ sk-or-...",
        "deepseek": "api.deepseek.com/v1 + ключ",
        "groq": "api.groq.com/openai/v1 + ключ",
        "together": "api.together.xyz/v1 + ключ",
        "mistral-api": "api.mistral.ai/v1 + ключ mstrl_...",
        "anthropic": "claude + ключ x-api (ANTHROPIC_API_KEY)",
    }
    PROVIDERS = list(PRESET_HINTS)

    def __init__(self, master: Tk, on_start) -> None:
        super().__init__(master, padding=12)
        self.on_start = on_start
        self.vars: dict[str, StringVar] = {}
        stored = load_config()
        title = ttk.Label(
            self,
            text="Аналитик данных - автономный агент",
            font=("TkDefaultFont", 13, "bold"),
        )
        title.grid(row=0, column=0, columnspan=3, sticky=W, pady=(0, 10))
        for row, (key, label, default) in enumerate(self.FIELDS, start=1):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky=W, pady=3)
            variable = StringVar(value=str(stored.get(key, default)))
            self.vars[key] = variable
            if key == "provider":
                widget = ttk.Combobox(
                    self,
                    textvariable=variable,
                    values=self.PROVIDERS,
                    width=38,
                )
                widget.bind("<<ComboboxSelected>>", lambda event: self._update_hint())
            else:
                show = "•" if key == "api_key" else None
                widget = ttk.Entry(self, textvariable=variable, width=40, show=show)
            widget.grid(row=row, column=1, sticky=W, pady=3)
        self.hint = ttk.Label(self, text="", foreground="#555")
        self.hint.grid(row=len(self.FIELDS) + 1, column=0, columnspan=2, sticky=W, pady=(2, 6))

        google_row = len(self.FIELDS) + 2
        self.google_status = ttk.Label(self, text=self._google_status_text(), foreground="#555")
        self.google_status.grid(row=google_row, column=0, columnspan=2, sticky=W)
        connect = ttk.Button(self, text="Подключить Google", command=self._connect_google)
        connect.grid(row=google_row, column=2, sticky=W, pady=3)
        help_button = ttk.Button(self, text="Как подключить?", command=self._show_google_help)
        help_button.grid(row=google_row + 1, column=0, sticky=W, pady=3)

        start = ttk.Button(self, text="Запустить чат", command=self._start)
        start.grid(row=google_row + 2, column=0, columnspan=3, pady=(14, 0))
        self.columnconfigure(1, weight=1)
        self._update_hint()

    # ---- helpers -----------------------------------------------------------
    def values(self) -> dict:
        config = {key: variable.get().strip() for key, variable in self.vars.items()}
        return {key: value for key, value in config.items() if value}

    def _google_status_text(self) -> str:
        token = app_dir() / "google_token.json"
        if token.exists():
            return "Google: подключён (google_token.json)"
        return "Google: не подключён (публичные файлы Drive работают и так)"

    def _show_google_help(self) -> None:
        messagebox.showinfo("Подключение Google Drive", GOOGLE_OAUTH_SETUP_STEPS)

    def _connect_google(self) -> None:
        credentials = app_dir() / "credentials.json"
        if not credentials.exists():
            self._show_google_help()
            return
        from . import oauth_setup

        def run() -> None:
            try:
                oauth_setup.main()
            except Exception as exc:
                try:
                    print(f"Google setup failed: {exc}", file=sys.stderr)
                except Exception:
                    pass

        threading.Thread(target=run, daemon=True).start()
        self._poll_google_token()

    def _poll_google_token(self) -> None:
        if (app_dir() / "google_token.json").exists():
            self.refresh_google_status()
            return
        self.after(1500, self._poll_google_token)

    def _update_hint(self) -> None:
        provider = self.vars["provider"].get().strip()
        self.hint["text"] = self.PRESET_HINTS.get(provider.lower().replace("_", "-"), "")

    def _start(self) -> None:
        config = self.values()
        save_config(config)
        try:
            client = make_client(config)
        except Exception as exc:
            messagebox.showerror("Аналитик", f"Модель не настроена:\n\n{exc}")
            return
        self.on_start(client, config["conversation"])

    def refresh_google_status(self) -> None:
        self.google_status["text"] = self._google_status_text()


def make_client(config: dict):
    from .agent import make_client as build

    base_url = config.get("base_url") or None
    if config.get("provider", "").startswith("http"):
        base_url = config["provider"]
    return build(
        config.get("provider", ""),
        config.get("model") or None,
        base_url,
        config.get("api_key") or None,
    )


class ChatWindow(ttk.Frame):
    def __init__(self, master: Tk, client, conversation) -> None:
        super().__init__(master, padding=8)
        self.master = master
        self.client = client
        self.conversation = conversation
        self.queue: "queue.Queue[tuple[str, str]]" = queue.Queue()

        header = ttk.Label(
            self,
            text=f"Диалог: {conversation.name}   |   память: {conversation.root}",
            foreground="#555",
        )
        header.pack(anchor=W)

        self.log = ttk.Text(self, wrap="word", state=DISABLED, width=72, height=24)
        self.log.pack(fill=BOTH, expand=True, pady=4)

        bottom = ttk.Frame(self)
        bottom.pack(fill=X, side=BOTTOM)
        self.entry = ttk.Entry(bottom, width=80)
        self.entry.pack(side=LEFT, fill=X, expand=True, padx=(0, 6))
        self.entry.bind("<Return>", self._send)
        self.send = ttk.Button(bottom, text="Отправить", command=self._send)
        self.send.pack(side=LEFT)
        ttk.Button(bottom, text="Настройки", command=self._open_settings).pack(side=LEFT, padx=(6, 0))
        self.busy = False
        self._welcome()

    def _open_settings(self) -> None:
        self.pack_forget()
        app = self.master
        if hasattr(app, "settings"):
            app.settings.refresh_google_status()
            app.settings.pack(fill=BOTH, expand=True)

    # ---- UI helpers --------------------------------------------------------
    def _welcome(self) -> None:
        if self.conversation.dataset is not None:
            note = f"Ранее загружен файл: {self.conversation.dataset.title} ({len(self.conversation.full)} строк)."
        else:
            note = "Пришлите ссылку на CSV из Google Drive (или https-ссылку/путь к файлу) и задавайте вопросы."
        self._append_line("Агент", note)
        self.entry.focus_set()

    def _append_line(self, who: str, text: str, color: str = "#000") -> None:
        tag = "grey" if color == "#666" else ("alert" if color == "#a00" else "normal")
        self.log["state"] = NORMAL
        self.log.tag_configure("grey", foreground="#666")
        self.log.tag_configure("alert", foreground="#aa0000")
        self.log.tag_configure("normal", foreground="#000000")
        self.log.insert(END, f"{who}: {text.strip()}\n\n", tag)
        self.log["state"] = DISABLED

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = DISABLED if busy else NORMAL
        self.send["state"] = state
        self.entry["state"] = state

    # ---- turn flow ---------------------------------------------------------
    def _send(self, event=None) -> None:
        if self.busy:
            return
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, END)
        self._append_line("Вы", text)
        self._set_busy(True)
        worker = threading.Thread(target=self._work, args=(text,), daemon=True)
        worker.start()
        self.after(100, self._poll)

    def _work(self, text: str) -> None:
        from .agent import run_turn

        try:
            answer = run_turn(self.conversation, self.client, text)
            self.queue.put(("ok", answer))
        except Exception as exc:
            self.queue.put(("fail", f"{type(exc).__name__}: {exc}"))

    def _poll(self) -> None:
        try:
            status, message = self.queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll)
            return
        if status == "ok":
            self._append_line("Агент", message)
        else:
            self._append_line("Ошибка", message, color="#a00")
        self._set_busy(False)


class App(Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Автономный аналитик данных")
        self.geometry("760x600")
        self.minsize(560, 420)
        self.chat: ChatWindow | None = None
        self.report_callback_exception = self._report_exception
        ttk.Label(
            self,
            text="Аналитик данных - автономный агент (без облака)",
            font=("TkDefaultFont", 12, "bold"),
        ).pack(anchor=W, padx=12, pady=(10, 2))
        self.settings = SettingsFrame(self, self._open_chat)
        self.settings.pack(fill=BOTH, expand=True)

    def _report_exception(self, exc_type, exc_value, exc_traceback) -> None:
        import traceback

        text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        try:
            (app_dir() / "gui_error.log").write_text(text, encoding="utf-8")
        except OSError:
            pass
        messagebox.showerror("Сбой в программе", text[-1500:])

    def _open_chat(self, client, conversation_name: str) -> None:
        from .memory import Conversation

        self.settings.pack_forget()
        conv = Conversation(conversation_name, app_dir() / "memory").load()
        self.chat = ChatWindow(self, client, conv)
        self.chat.pack(fill=BOTH, expand=True)

    def _back_to_settings(self) -> None:
        if self.chat is not None:
            self.chat.pack_forget()
            self.chat = None
        self.settings.refresh_google_status()
        self.settings.pack(fill=BOTH, expand=True)


def main() -> None:
    try:
        app = App()
    except Exception:
        import traceback

        text = traceback.format_exc()
        try:
            (app_dir() / "gui_error.log").write_text(text, encoding="utf-8")
        except OSError:
            pass
        try:
            root = Tk()
            root.withdraw()
            messagebox.showerror("Сбой при запуске", text[-1500:])
        except Exception:
            raise
        raise
    app.mainloop()


if __name__ == "__main__":
    main()
