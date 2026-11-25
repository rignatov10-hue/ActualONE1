"""Desktop GUI for the standalone analyst: settings for the model provider and
key, then a chat window. Standard tkinter, no new dependencies.

Run without any window: ``python gui_entry.py``. Built as a Windows EXE with
``make_exe.bat`` (PyInstaller); the EXE keeps its config and memory in the
folder it is placed in, so it is fully portable.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import (
    BOTH,
    BOTTOM,
    DISABLED,
    END,
    LEFT,
    NORMAL,
    NW,
    StringVar,
    Tk,
    W,
    X,
    Y,
    messagebox,
    ttk,
)

CONFIG_NAME = "gui_config.json"

BG = "#060608"
PANEL = "#0b0b10"
FIELD = "#07070b"
LINE = "#2b2b36"
FG = "#f2f2f5"
DIM = "#8a8a97"
AGENT_TXT = "#d4d4dc"
FONT_MAIN = ("Consolas", 10)
FONT_SMALL = ("Consolas", 9)
FONT_TITLE = ("Consolas", 16, "bold")
FONT_SUB = ("Consolas", 9)
FONT_USER = ("Consolas", 10, "bold")
FONT_AGENT = ("Consolas", 10)
FONT_ERR = ("Consolas", 10, "bold")


def gradient_photo(width: int, height: int, top: str = "#111119", bottom: str = "#060608") -> tk.PhotoImage:
    photo = tk.PhotoImage(width=width, height=height)
    c1 = tuple(int(top[i : i + 2], 16) for i in (1, 3, 5))
    c2 = tuple(int(bottom[i : i + 2], 16) for i in (1, 3, 5))
    for y in range(height):
        t = y / max(height - 1, 1)
        r, g, b = (round(a + (b_ - a) * t) for a, b_ in zip(c1, c2))
        photo.put(f"{{#{r:02x}{g:02x}{b:02x}}}", to=(0, y, width, y + 1))
    return photo


class GradientBanner(tk.Canvas):
    """Futuristic dark-gradient header strip with glowing bottom line."""

    def __init__(self, master, title: str, subtitle: str, height: int = 78) -> None:
        super().__init__(master, height=height, highlightthickness=0, bd=0, background=BG)
        self.banner_title = title
        self.banner_subtitle = subtitle
        self.photo: tk.PhotoImage | None = None
        self.bind("<Configure>", self._redraw)

    def _redraw(self, event=None) -> None:
        width = max(self.winfo_width(), 1)
        height = max(self.winfo_height(), 40)
        if width < 2:
            return
        self.delete("all")
        self.photo = gradient_photo(width, height + 40)
        self.create_image(0, 0, image=self.photo, anchor=NW)
        self.create_text(18, 16, anchor=NW, text=self.banner_title, fill=FG, font=FONT_TITLE)
        if self.banner_subtitle:
            self.create_text(18, 48, anchor=NW, text=self.banner_subtitle, fill=DIM, font=FONT_SUB)
        self.create_line(0, height - 1, width, height - 1, fill="#23232e")
        self.create_line(0, height, width, height, fill="#060608")


def apply_style(root) -> None:
    """Neon-black corporate theme (Cyberpunk-2077 style, monochrome)."""
    root.configure(background=BG)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(
        ".",
        background=BG,
        foreground=FG,
        font=FONT_MAIN,
        borderwidth=0,
        focuscolor=FG,
    )
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Title.TLabel", background=BG, foreground=FG, font=FONT_TITLE)
    style.configure("Sub.TLabel", background=BG, foreground=DIM, font=FONT_SUB)
    style.configure("Dim.TLabel", background=BG, foreground=DIM)
    style.configure(
        "TEntry",
        fieldbackground=FIELD,
        foreground=FG,
        insertcolor=FG,
        bordercolor=LINE,
        lightcolor=LINE,
        darkcolor=LINE,
        selectbackground=FG,
        selectforeground="#060608",
        padding=4,
    )
    style.map("TEntry", bordercolor=[("focus", FG)])
    style.configure(
        "TButton",
        background=PANEL,
        foreground=FG,
        bordercolor=LINE,
        lightcolor=LINE,
        darkcolor=LINE,
        focuscolor=FG,
        font=FONT_MAIN,
        padding=(12, 5),
    )
    style.map(
        "TButton",
        background=[("active", "#16161e"), ("pressed", "#1e1e28")],
        foreground=[("disabled", "#4a4a54")],
    )
    style.configure(
        "Accent.TButton",
        background=FG,
        foreground="#060608",
        bordercolor=FG,
        focuscolor="#060608",
        font=("Consolas", 11, "bold"),
        padding=(18, 7),
    )
    style.map(
        "Accent.TButton",
        background=[("active", "#d8d8de"), ("pressed", "#c2c2ca")],
        foreground=[("disabled", "#3a3a42")],
    )
    style.configure(
        "TCombobox",
        fieldbackground=FIELD,
        background=PANEL,
        foreground=FG,
        arrowcolor=FG,
        bordercolor=LINE,
        lightcolor=LINE,
        darkcolor=LINE,
        selectbackground=FIELD,
        selectforeground=FG,
        padding=4,
    )
    style.map(
        "TCombobox",
        bordercolor=[("focus", FG)],
        fieldbackground=[("readonly", FIELD)],
        foreground=[("readonly", FG)],
    )
    root.option_add("*TCombobox*Listbox.background", PANEL)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", FG)
    root.option_add("*TCombobox*Listbox.selectForeground", "#060608")
    root.option_add("*TCombobox*Listbox.font", FONT_MAIN)
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
        super().__init__(master, padding=(16, 4))
        self.on_start = on_start
        self.vars: dict[str, StringVar] = {}
        stored = load_config()
        for row, (key, label, default) in enumerate(self.FIELDS, start=0):
            ttk.Label(self, text=label, style="Dim.TLabel").grid(row=row, column=0, sticky=W, pady=4)
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
        self.hint = ttk.Label(self, text="", style="Dim.TLabel")
        self.hint.grid(row=len(self.FIELDS), column=0, columnspan=2, sticky=W, pady=(2, 8))

        google_row = len(self.FIELDS) + 1
        self.google_status = ttk.Label(self, text=self._google_status_text(), style="Dim.TLabel")
        self.google_status.grid(row=google_row, column=0, columnspan=2, sticky=W)
        connect = ttk.Button(self, text="ПОДКЛЮЧИТЬ GOOGLE", command=self._connect_google)
        connect.grid(row=google_row, column=2, sticky=W, pady=4)
        help_button = ttk.Button(self, text="КАК ПОДКЛЮЧИТЬ?", command=self._show_google_help)
        help_button.grid(row=google_row + 1, column=0, sticky=W, pady=4)

        self.skills_status = ttk.Label(self, text=self._skills_status_text(), style="Dim.TLabel")
        self.skills_status.grid(row=google_row + 1, column=1, sticky=W, pady=4)
        skills_button = ttk.Button(self, text="ПАПКА НАВЫКОВ", command=self._open_skills_folder)
        skills_button.grid(row=google_row + 1, column=2, sticky=W, pady=4)

        self.pc_var = tk.BooleanVar(value=str(stored.get("pc_tools")) == "1")
        ttk.Checkbutton(
            self,
            text="О П К // Разрешить управление этим ПК (команды, файлы, запуск VM; журнал: pc_log.jsonl)",
            variable=self.pc_var,
        ).grid(row=google_row + 2, column=0, columnspan=3, sticky=W, pady=(6, 0))
        self.no_confirm_var = tk.BooleanVar(value=str(stored.get("pc_no_confirm")) == "1")
        ttk.Checkbutton(
            self,
            text="БЕЗ ПОДТВЕРЖДЕНИЙ // выполнять действия сразу (авто-Да, всё в журнале)",
            variable=self.no_confirm_var,
        ).grid(row=google_row + 3, column=0, columnspan=3, sticky=W, pady=(2, 6))

        start = ttk.Button(self, text=">>  ЗАПУСТИТЬ", style="Accent.TButton", command=self._start)
        start.grid(row=google_row + 4, column=0, columnspan=3, pady=(18, 8))
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

    def _skills_status_text(self) -> str:
        from .skills import discover_skills

        skills = discover_skills()
        return f"Навыки: {len(skills)} (папка skills рядом с программой)"

    def _open_skills_folder(self) -> None:
        from .skills import ensure_example_skill

        root = ensure_example_skill()
        try:
            if hasattr(os, "startfile"):
                os.startfile(root)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(root)])
        except Exception:
            messagebox.showinfo("Навыки", f"Папка навыков:\n{root}")

    def _update_hint(self) -> None:
        provider = self.vars["provider"].get().strip()
        self.hint["text"] = self.PRESET_HINTS.get(provider.lower().replace("_", "-"), "")

    def _start(self) -> None:
        from . import pc as pc_mod

        pc_mod.set_enabled(bool(self.pc_var.get()))
        pc_mod.set_confirm_mode(bool(self.no_confirm_var.get()))
        config = self.values()
        config["pc_tools"] = "1" if self.pc_var.get() else ""
        config["pc_no_confirm"] = "1" if self.no_confirm_var.get() else ""
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
        super().__init__(master, padding=10)
        self.master = master
        self.client = client
        self.conversation = conversation
        self.queue: "queue.Queue[tuple[str, str]]" = queue.Queue()

        GradientBanner(
            self,
            "ACTUALONE",
            f">> диалог: {conversation.name}   //   память: {conversation.root}",
            height=70,
        ).pack(fill=X, pady=(0, 4))

        console = ttk.Frame(self)
        console.pack(fill=BOTH, expand=True, pady=4)
        self.log = tk.Text(
            console,
            wrap="word",
            state=DISABLED,
            width=72,
            height=24,
            background=FIELD,
            foreground=AGENT_TXT,
            insertbackground=FG,
            font=FONT_AGENT,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=LINE,
            highlightcolor=FG,
            selectbackground=FG,
            selectforeground="#060608",
            spacing1=2,
            spacing3=4,
        )
        self.log.pack(side=LEFT, fill=BOTH, expand=True)
        scroll = ttk.Scrollbar(console, command=self.log.yview)
        scroll.pack(side=LEFT, fill=Y)
        self.log.configure(yscrollcommand=scroll.set)

        bottom = ttk.Frame(self)
        bottom.pack(fill=X, side=BOTTOM)
        self.entry = ttk.Entry(bottom, font=FONT_MAIN)
        self.entry.pack(side=LEFT, fill=X, expand=True, padx=(0, 6), ipady=2)
        self.entry.bind("<Return>", self._send)
        self.send = ttk.Button(bottom, text="ОТПРАВИТЬ", style="Accent.TButton", command=self._send)
        self.send.pack(side=LEFT)
        ttk.Button(bottom, text="НАСТРОЙКИ", command=self._open_settings).pack(side=LEFT, padx=(6, 0))
        self.busy = False
        conversation.approval_hook = self._ask_approval
        conversation.activity_hook = self._report_activity
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
            note = (
                "Готов работать. Просто задавайте вопросы; если нужна работа с данными — "
                "пришлите ссылку на CSV/Excel (Google Drive) или путь к файлу на диске."
            )
        self._append_line("Агент", note)
        self.entry.focus_set()

    def _append_line(self, who: str, text: str, color: str = "#000") -> None:
        tag = {"#000": "agent", "#666": "sys", "#a00": "err"}.get(color, "agent")
        self.log["state"] = NORMAL
        self.log.tag_configure("user", foreground=FG, font=FONT_USER)
        self.log.tag_configure("agent", foreground=AGENT_TXT, font=FONT_AGENT)
        self.log.tag_configure("sys", foreground=DIM, font=FONT_SMALL)
        self.log.tag_configure("err", foreground=FG, background="#1c1c24", font=FONT_ERR)
        prefix = f"[{who}] "
        self.log.insert(END, prefix, ("user" if who == "Вы" else tag),)
        self.log.insert(END, f"{text.strip()}\n\n", tag)
        self.log.see(END)
        self.log["state"] = DISABLED

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.send["state"] = DISABLED if busy else NORMAL
        self.send["text"] = "ДУМАЮ…" if busy else "ОТПРАВИТЬ"

    def _ask_approval(self, question: str) -> bool:
        result = {"value": False}
        done = threading.Event()

        def ask() -> None:
            try:
                result["value"] = messagebox.askyesno("ПОДТВЕРЖДЕНИЕ ДЕЙСТВИЯ", question)
            finally:
                done.set()

        self.master.after(0, ask)
        done.wait(timeout=600)
        return result["value"]

    def _report_activity(self, text: str) -> None:
        self.queue.put(("activity", text))

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
        elif status == "activity":
            self._append_line("Система", message, color="#666")
            self.after(100, self._poll)
            return
        else:
            self._append_line("Ошибка", message, color="#a00")
        self._set_busy(False)


class App(Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ActualONE")
        self.geometry("780x620")
        self.minsize(560, 440)
        self.configure(background=BG)
        self.chat: ChatWindow | None = None
        self.report_callback_exception = self._report_exception
        apply_style(self)
        GradientBanner(
            self,
            "ACTUALONE",
            ">> автономный агент // локальная память // любые модели",
        ).pack(fill=X)
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
        self.title("ActualONE")
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
