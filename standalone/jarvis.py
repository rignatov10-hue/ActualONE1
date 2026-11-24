"""JARVIS mode: a separate voice-assistant face of the analyst.

Works on top of the same model client and the same memory as the chat mode:
any provider (KoboldCpp, Ollama, OpenAI, OpenRouter, ...) plus local files.
Voice parts (edge-tts, pygame, vosk, speech_recognition, pyautogui, psutil)
are optional and imported lazily; without them JARVIS still works as a
text assistant.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import platform
import queue
import re
import subprocess
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import pc as pc_mod

WAKE_WORDS = ("джарвис", "jarvis", "жарвис", "джарвиз", "джервис")
LANGUAGE = "ru-RU"
VOICE = "ru-RU-DmitryNeural"
VOICE_RATE = "+30%"
VOICE_PITCH = "-8Hz"
HISTORY_LIMIT = 12
EXIT_WORDS = {"отбой", "выход", "пока"}
SLEEP_RE = re.compile(r"режим ожидания|не слушай|помолчи|усни")

JARVIS_PROMPT = """Ты — ДЖАРВИС, ИИ-дворецкий. Говори по-русски, обращайся "сэр",
отвечай 1-2 короткими предложениями обычным текстом, без markdown.
Если нужно действие на компьютере, добавь в конце отдельной строкой:
ACTION: {"type": "...", "args": {...}}
Действия: open_app{name: chrome|notepad|calc|explorer|spotify|code|terminal},
open_url{url}, search_web{query}, type_text{text}, press_keys{keys: ["ctrl","t"]},
media{command: playpause|next|prev|mute|volup|voldown}, close_window, screenshot,
system_info, lock_screen, shell{command}.
Пример. Просьба: сделай скриншот. Ответ:
Делаю снимок, сэр.
ACTION: {"type": "screenshot", "args": {}}"""


def voice_cache_dir() -> Path:
    from .gui import app_dir

    return app_dir() / "voice_cache"


# ---- protocol parsing (pure) -----------------------------------------------


def parse_actions(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Split raw model output into spoken text and ACTION dicts."""
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict):
            spoken = str(data.get("say", ""))
            act = data.get("action")
            acts = [act] if isinstance(act, dict) and act.get("type") else []
            return spoken, acts
    actions: list[dict[str, Any]] = []
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        match = re.match(r"(?i)^ACTION:\s*(\{.*\})\s*$", stripped)
        if match:
            try:
                act = json.loads(match.group(1))
            except ValueError:
                act = None
            if isinstance(act, dict) and act.get("type"):
                actions.append(act)
            continue
        lines.append(line)
    clean = "\n".join(lines).strip()
    return clean, actions


def split_wake(text: str) -> tuple[bool, str]:
    words = text.split()
    for i, word in enumerate(words):
        clean = word.strip(",.!?:;")
        if clean in WAKE_WORDS or difflib.get_close_matches(clean, WAKE_WORDS, n=1, cutoff=0.75):
            return True, " ".join(words[:i] + words[i + 1:]).strip(" ,.")
    return False, text


def match_rules(text: str) -> tuple[str, list[dict[str, Any]]] | None:
    """Quick deterministic commands, answered without the model."""
    s = text.lower().replace("ё", "е").strip()
    if not s:
        return None
    media = [
        (r"без звука|выключи звук|отключи звук", "mute", "Звук отключён, сэр."),
        (r"громче|прибавь звук|увеличь громкость", "volup", "Громче, сэр."),
        (r"тише|убавь звук|уменьши громкость", "voldown", "Тише, сэр."),
        (r"пауза|продолжи|воспроизведи|играй", "playpause", "Выполняю, сэр."),
        (r"следующ", "next", "Следующий трек, сэр."),
        (r"предыдущ|прошлый трек", "prev", "Предыдущий трек, сэр."),
    ]
    for pattern, command, phrase in media:
        if re.search(pattern, s):
            return phrase, [{"type": "media", "args": {"command": command}}]
    if re.search(r"скриншот|снимок экрана", s):
        return "Делаю снимок, сэр.", [{"type": "screenshot", "args": {}}]
    if re.search(r"закрой (окно|вкладку)", s):
        return "Закрываю, сэр.", [{"type": "close_window", "args": {}}]
    match = re.search(r"(?:открой|запусти|включи|зайди на|зайди в)\s+(.+)", s)
    if match:
        target = match.group(1).strip()
        for app, aliases in APP_ALIASES.items():
            if any(alias in target for alias in aliases):
                return "Открываю, сэр.", [{"type": "open_app", "args": {"name": app}}]
        return "Открываю, сэр.", [{"type": "search_web", "args": {"query": target}}]
    if re.search(r"^(привет|здравствуй|доброе утро|добрый день|добрый вечер)", s):
        return "Приветствую, сэр. Чем могу быть полезен?", []
    if re.search(r"как (у тебя |твои )?дела|как ты\b", s):
        return "Все системы в норме, сэр.", []
    if re.search(r"спасибо|благодарю", s):
        return "Всегда к вашим услугам, сэр.", []
    return None


APP_ALIASES = {
    "chrome": ("хром", "chrome", "браузер", "google"),
    "notepad": ("блокнот", "notepad"),
    "calc": ("калькулятор", "calc"),
    "explorer": ("проводник", "папку", "explorer"),
    "spotify": ("спотифай", "spotify", "музыку"),
    "code": ("код", "vs code", "vscode", "студию"),
    "terminal": ("терминал", "консоль", "cmd"),
}


# ---- voice output (optional edge-tts + pygame) ------------------------------


class Voice:
    def __init__(self) -> None:
        self.synth_q: "queue.Queue[str]" = queue.Queue()
        self.play_q: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.pending = 0
        self.recent: deque[str] = deque(maxlen=5)
        self.cv = threading.Condition()
        self.enabled = True
        threading.Thread(target=self._synth_loop, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    def say(self, text: str) -> None:
        text = re.sub(r"[*_`#>]", "", text).strip()
        if not text:
            return
        self.recent.append(re.sub(r"[^\w\s]", "", text.lower()))
        for sentence in [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text) if re.search(r"\w", s)]:
            if not self.enabled:
                print(f"JARVIS: {sentence}", flush=True)
                continue
            with self.cv:
                self.pending += 1
            self.synth_q.put(sentence)

    def wait(self) -> None:
        with self.cv:
            self.cv.wait_for(lambda: self.pending == 0)

    def _synth_loop(self) -> None:
        while True:
            text = self.synth_q.get()
            try:
                path = self._synth_file(text)
            except Exception as exc:
                print(f"[голос недоступен: {exc}]", flush=True)
                path = None
            self.play_q.put((text, path))

    def _synth_file(self, text: str):
        import asyncio

        import edge_tts

        cache = voice_cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        key = hashlib.md5(f"{text}|{VOICE}|{VOICE_RATE}|{VOICE_PITCH}".encode()).hexdigest()
        mp3 = cache / f"{key}.mp3"
        if mp3.exists():
            return str(mp3)
        tmp = mp3.with_suffix(f".mp3.{threading.get_ident()}.part")
        asyncio.run(edge_tts.Communicate(text, VOICE, rate=VOICE_RATE, pitch=VOICE_PITCH).save(str(tmp)))
        if tmp.stat().st_size > 0:
            os.replace(tmp, mp3)
            return str(mp3)
        raise RuntimeError("Пустой ответ синтезатора речи.")

    def _play_loop(self) -> None:
        while True:
            text, path = self.play_q.get()
            try:
                self._play(text, path)
            except Exception as exc:
                print(f"[ошибка воспроизведения: {exc}]", flush=True)
            finally:
                with self.cv:
                    self.pending -= 1
                    self.cv.notify_all()

    def _play(self, text: str, path) -> None:
        if path is None:
            print(f"JARVIS: {text}", flush=True)
            return
        try:
            import pygame

            if not pygame.mixer.get_init():
                pygame.mixer.init()
            sound = pygame.mixer.Sound(path)
            length = sound.get_length()
            channel = sound.play()
            busy = channel.get_busy if channel else (lambda: False)
        except Exception:
            pygame_error = True
        else:
            pygame_error = False
        if pygame_error:
            print(f"JARVIS: {text}", flush=True)
            return
        sys_write_synced(text, length, busy)


def sys_write_synced(text: str, length: float, busy: Callable[[], bool]) -> None:
    import sys

    sys.stdout.write("JARVIS: ")
    sys.stdout.flush()
    delay = length * 0.92 / max(len(text), 1)
    start = time.time()
    for i, ch in enumerate(text):
        sys.stdout.write(ch)
        sys.stdout.flush()
        pause = start + (i + 1) * delay - time.time()
        if pause > 0:
            time.sleep(pause)
    sys.stdout.write("\n")
    sys.stdout.flush()
    while busy():
        time.sleep(0.02)


# ---- voice input (optional vosk -> Google STT) ------------------------------


class Listener:
    def __init__(self) -> None:
        self.model = None
        self.recognizer = None
        self.reason = ""

    def init(self) -> bool:
        try:
            import speech_recognition as sr
        except ImportError:
            self.reason = "нет speech_recognition (pip install SpeechRecognition pyaudio)"
            return False
        self.recognizer = sr.Recognizer()
        self.recognizer.pause_threshold = 0.4
        self.recognizer.non_speaking_duration = 0.25
        self.recognizer.operation_timeout = 10
        if self._init_vosk():
            return True
        return True

    def _init_vosk(self) -> bool:
        try:
            from vosk import Model, SetLogLevel
        except ImportError:
            self.reason = "нет vosk — использую Google STT (медленнее)"
            return False
        try:
            import urllib.request
            import zipfile

            SetLogLevel(-1)
            name = "vosk-model-small-ru-0.22"
            from .gui import app_dir

            base = app_dir()
            path = base / name
            if not path.is_dir():
                url = f"https://alphacephei.com/vosk/models/{name}.zip"
                zip_path = path.with_suffix(".zip")
                urllib.request.urlretrieve(url, zip_path)
                with zipfile.ZipFile(zip_path) as bundle:
                    bundle.extractall(base)
                os.remove(zip_path)
            self.model = Model(str(path))
            return True
        except Exception as exc:
            self.reason = f"vosk не запустился ({exc}) — использую Google STT"
            self.model = None
            return False

    def listen(self, timeout: int | None = None, phrase_limit: int = 12) -> str:
        import speech_recognition as sr

        try:
            with sr.Microphone() as source:
                if self.model is None:
                    audio = self.recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_limit)
                    try:
                        text = self.recognizer.recognize_google(audio, language=LANGUAGE)
                    except sr.UnknownValueError:
                        return ""
                    except sr.RequestError as exc:
                        print(f"[распознавание недоступно: {exc}]", flush=True)
                        return ""
                else:
                    text = self._listen_vosk(source, timeout, phrase_limit)
        except sr.WaitTimeoutError:
            return ""
        except OSError as exc:
            print(f"[ошибка микрофона: {exc}]", flush=True)
            time.sleep(1)
            return ""
        return text.lower().replace("ё", "е").strip()

    def _listen_vosk(self, source, timeout: int | None, phrase_limit: int) -> str:
        from vosk import KaldiRecognizer

        rec = KaldiRecognizer(self.model, 16000)
        time.sleep(0.2)
        start, speech_start = time.time(), None
        while True:
            data = source.stream.read(2000)
            if rec.AcceptWaveform(data):
                text = json.loads(rec.Result()).get("text", "")
                if text:
                    return text
                speech_start = None
            elif speech_start is None and json.loads(rec.PartialResult()).get("partial"):
                speech_start = time.time()
            now = time.time()
            if speech_start is None:
                if timeout and now - start > timeout:
                    return ""
                if not timeout and now - start > 60:
                    rec, start = KaldiRecognizer(self.model, 16000), now
            elif now - speech_start > phrase_limit:
                return json.loads(rec.FinalResult()).get("text", "")


# ---- actions ----------------------------------------------------------------


def is_echo(text: str, recent: deque[str]) -> bool:
    words = text.split()
    if len(words) < 2:
        return False
    return any(difflib.SequenceMatcher(None, text, other).ratio() > 0.7 for other in recent)


class Assistant:
    def __init__(self, client: Any, conv: Any, emit: Callable[[str, str], None] | None = None) -> None:
        self.client = client
        self.conv = conv
        self.emit = emit or (lambda who, text: None)
        self.voice = Voice()
        self.history: deque[dict[str, str]] = deque(maxlen=HISTORY_LIMIT)
        self.recent_heard: deque[str] = deque(maxlen=5)

    # -- one command in, spoken reply out ------------------------------------
    def handle(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        rule = match_rules(text)
        if rule is not None:
            phrase, actions = rule
            self.voice.say(phrase)
            self.emit("Джарвис", phrase)
            self._run_actions(actions)
            return phrase
        if len(text.split()) < 2:
            phrase = "Не понял команду, сэр."
            self.voice.say(phrase)
            self.emit("Джарвис", phrase)
            return phrase
        return self._ask_model(text)

    def _ask_model(self, text: str) -> str:
        self.history.append({"role": "user", "content": text})
        messages = list(self.history)
        try:
            answer = self.client.complete(JARVIS_PROMPT, messages, [])
        except Exception as exc:
            self.history.pop()
            phrase = f"Модель ответила ошибкой: {exc}"
            self.voice.say(phrase)
            self.emit("Ошибка", phrase)
            return phrase
        self.history.append({"role": "assistant", "content": answer})
        spoken, actions = parse_actions(answer)
        if not spoken and not actions:
            spoken = "Не понял команду, сэр."
        if spoken:
            self.voice.say(spoken)
            self.emit("Джарвис", spoken)
        self._run_actions(actions)
        return spoken

    # -- actions ---------------------------------------------------------------
    def _run_actions(self, actions: list[dict[str, Any]]) -> None:
        for act in actions:
            note = self.execute(act)
            if note:
                self.voice.say(note)
                self.emit("Система", note)

    def execute(self, action: dict[str, Any]) -> str:
        kind = action.get("type", "")
        args = action.get("args") or {}
        try:
            handler = getattr(self, f"_do_{kind}", None)
            if handler is None:
                return f"Неизвестное действие: {kind}, сэр."
            return handler(args) or ""
        except Exception as exc:
            return f"Ошибка действия {kind}: {exc}"

    def _pc_allowed(self) -> bool:
        return pc_mod.enabled()

    def _do_open_app(self, args: dict[str, Any]) -> str:
        name = str(args.get("name", "")).lower()
        table = {
            "Windows": {
                "chrome": "start chrome", "notepad": "start notepad", "calc": "start calc",
                "explorer": "start explorer", "spotify": "start spotify:", "code": "code",
                "terminal": "start cmd",
            },
            "Darwin": {
                "chrome": "open -a 'Google Chrome'", "notepad": "open -a TextEdit",
                "calc": "open -a Calculator", "explorer": "open .",
                "spotify": "open -a Spotify", "code": "open -a 'Visual Studio Code'",
                "terminal": "open -a Terminal",
            },
            "Linux": {
                "chrome": "google-chrome", "notepad": "gedit", "calc": "gnome-calculator",
                "explorer": "xdg-open .", "spotify": "spotify", "code": "code",
                "terminal": "x-terminal-emulator",
            },
        }
        command = table.get(platform.system(), {}).get(name)
        if not command:
            return f"Не знаю приложение {name}, сэр."
        if not self._pc_allowed():
            return "Управление ПК выключено, сэр (галочка О П К)."
        subprocess.Popen(command, shell=True)
        return ""

    def _do_open_url(self, args: dict[str, Any]) -> str:
        url = str(args.get("url", "")).strip()
        if not re.match(r"https?://", url):
            return "Неправильный адрес, сэр."
        webbrowser.open(url)
        return ""

    def _do_search_web(self, args: dict[str, Any]) -> str:
        from urllib.parse import quote

        query = str(args.get("query", "")).strip()
        if query:
            webbrowser.open(f"https://www.google.com/search?q={quote(query)}")
        return ""

    def _autogui(self):
        if not self._pc_allowed():
            raise PermissionError("Управление ПК выключено (галочка О П К).")
        import pyautogui

        pyautogui.FAILSAFE = True
        return pyautogui

    def _do_type_text(self, args: dict[str, Any]) -> str:
        self._autogui().typewrite(str(args.get("text", "")), interval=0.01)
        return ""

    def _do_press_keys(self, args: dict[str, Any]) -> str:
        keys = args.get("keys") or []
        if isinstance(keys, str):
            keys = [keys]
        self._autogui().hotkey(*[str(k) for k in keys])
        return ""

    def _do_media(self, args: dict[str, Any]) -> str:
        command = str(args.get("command", ""))
        combos = {
            "playpause": ["playpause"], "next": ["nexttrack"], "prev": ["prevtrack"],
            "mute": ["volumemute"], "volup": ["volumeup"], "voldown": ["volumedown"],
        }
        if command not in combos:
            return f"Не знаю медиа-команду {command}, сэр."
        self._autogui().press(combos[command][0])
        return ""

    def _do_screenshot(self, args: dict[str, Any]) -> str:
        auto = self._autogui()
        from .gui import app_dir

        shots = app_dir() / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        path = shots / f"shot-{datetime.now():%Y%m%d-%H%M%S}.png"
        auto.screenshot(str(path))
        return f"Снимок: {path.name}, сэр."

    def _do_close_window(self, args: dict[str, Any]) -> str:
        self._autogui().hotkey("alt", "f4")
        return ""

    def _do_lock_screen(self, args: dict[str, Any]) -> str:
        if not self._pc_allowed():
            return "Управление ПК выключено, сэр (галочка О П К)."
        if platform.system() == "Windows":
            ctypes_hook()
            return "Блокирую, сэр."
        return "Блокировка поддержана только на Windows, сэр."

    def _do_system_info(self, args: dict[str, Any]) -> str:
        info = [f"{platform.system()} {platform.release()}", f"CPU: {platform.machine()}"]
        try:
            import psutil

            memory = psutil.virtual_memory()
            battery = psutil.sensors_battery()
            info.append(f"RAM: {memory.percent}% занято")
            if battery:
                info.append(f"Батарея: {battery.percent}%")
        except Exception:
            pass
        return ", ".join(info)

    def _do_shell(self, args: dict[str, Any]) -> str:
        if not self._pc_allowed():
            return "Управление ПК выключено, сэр (галочка О П К)."
        result = pc_mod.run_shell(self.conv, str(args.get("command", "")))
        out = (result.get("stdout") or result.get("stderr") or "").strip()
        return f"Готово, код {result.get('returncode')}. {out[:300]}" if out else f"Готово, код {result.get('returncode')}, сэр."

    def _do_shutdown(self, args: dict[str, Any]) -> str:
        if pc_mod.confirm_mode() != "auto":
            hook = getattr(self.conv, "approval_hook", None)
            if hook is None or not hook("Выключить компьютер?"):
                return "Выключение отменено, сэр."
        if platform.system() == "Windows":
            subprocess.Popen("shutdown /s /t 5", shell=True)
            return "Выключаю через 5 секунд, сэр."
        return "Выключение поддержано только на Windows, сэр."


def ctypes_hook() -> None:
    import ctypes

    ctypes.windll.user32.LockWorkStation()


# ---- voice loop -------------------------------------------------------------


def voice_loop(assistant: Assistant, stop: threading.Event, listener: Listener) -> str:
    """Listen-speak cycle; returns 'exit' when the user says goodbye."""
    phrase = "Системы запущены. Слушаю вас, сэр."
    assistant.voice.say(phrase)
    assistant.emit("Джарвис", phrase)
    sleeping = False
    while not stop.is_set():
        try:
            heard = listener.listen(phrase_limit=10)
        except Exception as exc:
            print(f"[listening error: {exc}]", flush=True)
            time.sleep(1)
            continue
        if stop.is_set():
            break
        if not heard or is_echo(heard, assistant.voice.recent) or is_echo(heard, assistant.recent_heard):
            continue
        assistant.recent_heard.append(heard)
        woke, command = split_wake(heard)
        if sleeping:
            if not woke:
                continue
            sleeping = False
        if woke and not command:
            assistant.voice.say("Да, сэр?")
            assistant.voice.wait()
            command = listener.listen(timeout=8)
            if not command or is_echo(command, assistant.voice.recent):
                continue
        if EXIT_WORDS & set(command.split()) or "до свидания" in command:
            phrase = "Хорошего дня, сэр."
            assistant.voice.say(phrase)
            assistant.emit("Джарвис", phrase)
            assistant.voice.wait()
            return "exit"
        if SLEEP_RE.search(command):
            sleeping = True
            phrase = "Перехожу в режим ожидания. Назовите моё имя, когда понадоблюсь, сэр."
            assistant.voice.say(phrase)
            assistant.emit("Джарвис", phrase)
            assistant.voice.wait()
            continue
        assistant.handle(command)
        assistant.voice.wait()
    return "stopped"


def terminal_loop(client: Any, conv: Any) -> None:
    """JARVIS in the terminal: voice if deps exist, text always."""
    assistant = Assistant(client, conv)
    listener = Listener()
    mic_ok = listener.init()
    if listener.reason:
        print(f"[{listener.reason}]", flush=True)

    def emit(who: str, text: str) -> None:
        if who != "Джарвис":
            print(f"{who}: {text}", flush=True)

    if mic_ok:
        stop = threading.Event()
        worker = threading.Thread(target=voice_loop, args=(assistant, stop, listener), daemon=True)
        worker.start()
        print("Джарвис слушает микрофон. Напишите команду или Ctrl+C для выхода.", flush=True)
        try:
            while worker.is_alive():
                text = input()
                if text.strip():
                    assistant.handle(text)
                    assistant.voice.wait()
        except (KeyboardInterrupt, EOFError):
            stop.set()
        return
    print("Джарвис в текстовом режиме (нужны SpeechRecognition+pyaudio). Пустая строка — выход.", flush=True)
    while True:
        try:
            text = input("Вы: ")
        except (KeyboardInterrupt, EOFError):
            return
        if not text.strip():
            return
        assistant.handle(text)


__all__ = [
    "Assistant",
    "JARVIS_PROMPT",
    "Listener",
    "Voice",
    "match_rules",
    "parse_actions",
    "split_wake",
    "terminal_loop",
    "voice_loop",
]
