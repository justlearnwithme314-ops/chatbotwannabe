#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PERSONAL API HUB v2
===================

Proof-of-concept local AI/API control panel.

Zero third-party Python packages.
Requires only Python 3.x with tkinter (usually included with desktop Python).

Run:
    python personal_api_hub.py

Everything is stored beside this file:
    personal_api_hub_data.json
    personal_api_hub_logs.jsonl

Features
--------
CHAT
- Chat-style interface
- Provider dropdown
- Auto routing
- New/clear/export/import chat
- Streaming for OpenAI-compatible APIs when possible
- System prompt
- Temperature / max token controls
- Conversation history

APIS
- Built-in API editor
- Add/delete/duplicate providers
- Test provider
- Enable/disable providers
- Headers editor
- Arbitrary JSON body template
- Response extraction path
- Environment-variable expansion
- OpenAI-compatible provider mode
- Generic JSON REST provider mode
- GET/POST/PUT/PATCH/DELETE

ROUTER
- Rules based on words
- Regex rules
- Manual provider selection
- Default provider
- Failover chain
- Routing test panel

LOGS
- Search/filter
- Request/response/error events
- Export logs
- Clear logs
- Inspect selected event JSON

TOOLS
- Register local Python tools in code
- Enable/disable tools
- Built-in calculator
- Current time
- UUID
- JSON pretty-print
- Encode/decode URL
- Base64
- SHA-256
- HTTP GET fetch
- Local command execution (disabled by default in GUI listing; function exists as POC)

SETTINGS
- System prompt
- History size
- Timeout
- SSL verification
- Streaming
- Autosave

IMPORTANT
---------
This is intentionally a LOCAL proof of concept.
It is not hardened for exposing to a network.
The API editor can store headers/secrets in plaintext JSON.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk


# ==============================================================
# FILES / DEFAULTS
# ==============================================================

APP_DIR = Path(__file__).resolve().parent
DATA_FILE = APP_DIR / "personal_api_hub_data.json"
LOG_FILE = APP_DIR / "personal_api_hub_logs.jsonl"


DEFAULT_DATA = {
    "settings": {
        "system_prompt": (
            "You are my personal AI assistant. Be useful, direct, practical, "
            "and detailed when needed."
        ),
        "history_limit": 40,
        "timeout": 120,
        "verify_ssl": True,
        "stream": True,
        "autosave": True,
    },
    "providers": {
        "openai_example": {
            "type": "openai_compatible",
            "enabled": False,
            "base_url": "https://api.openai.com/v1",
            "endpoint": "/chat/completions",
            "model": "YOUR_MODEL",
            "api_key": "",
            "api_key_env": "OPENAI_API_KEY",
            "headers": {
                "Content-Type": "application/json"
            },
            "temperature": 0.7,
            "max_tokens": 2048,
            "extra_body": {}
        },
        "generic_json_example": {
            "type": "rest_json",
            "enabled": False,
            "url": "https://example.com/api/chat",
            "method": "POST",
            "headers": {
                "Content-Type": "application/json"
            },
            "body_template": {
                "prompt": "{{last_user_message}}",
                "messages": "{{messages}}"
            },
            "response_path": "response"
        }
    },
    "routing": {
        "default": "openai_example",
        "fallbacks": [],
        "rules": [
            {
                "name": "coding",
                "provider": "openai_example",
                "contains_any": [
                    "python", "code", "debug", "godot", "sql",
                    "javascript", "typescript", "programming"
                ],
                "contains_all": [],
                "regex": ""
            }
        ]
    },
    "chats": {
        "Default": []
    }
}


# ==============================================================
# BASIC HELPERS
# ==============================================================

def deep_copy(obj: Any) -> Any:
    return json.loads(json.dumps(obj))


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return deep_copy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return deep_copy(default)


def save_json(path: Path, obj: Any) -> None:
    path.write_text(
        json.dumps(obj, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def expand_env(value: Any) -> Any:
    """
    Supports:
        ${HOME}
        ${OPENAI_API_KEY}
        $OPENAI_API_KEY
    """
    if isinstance(value, str):
        pattern = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

        def repl(m: re.Match) -> str:
            key = m.group(1) or m.group(2)
            return os.getenv(key, m.group(0))

        return pattern.sub(repl, value)

    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}

    if isinstance(value, list):
        return [expand_env(v) for v in value]

    return value


def nested_get(data: Any, path: str, default: Any = None) -> Any:
    if not path:
        return data

    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return cur


def nested_set(data: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def pretty_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


# ==============================================================
# DATA MODELS
# ==============================================================

@dataclass
class Message:
    role: str
    content: str
    timestamp: str = field(default_factory=now)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderResponse:
    text: str
    provider: str
    model: str = ""
    raw: Any = None
    usage: Dict[str, Any] = field(default_factory=dict)


# ==============================================================
# LOGGING
# ==============================================================

class AppLogger:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, event: str, **data: Any) -> None:
        row = {
            "timestamp": now(),
            "event": event,
            **data,
        }
        try:
            with self.lock:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def read_all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []

        result: List[Dict[str, Any]] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        result.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass
        return result

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass


# ==============================================================
# STORAGE
# ==============================================================

class Store:
    def __init__(self):
        self.data = load_json(DATA_FILE, DEFAULT_DATA)

    def save(self) -> None:
        save_json(DATA_FILE, self.data)

    @property
    def settings(self) -> Dict[str, Any]:
        return self.data.setdefault("settings", {})

    @property
    def providers(self) -> Dict[str, Dict[str, Any]]:
        return self.data.setdefault("providers", {})

    @property
    def routing(self) -> Dict[str, Any]:
        return self.data.setdefault("routing", {})

    @property
    def chats(self) -> Dict[str, List[Dict[str, Any]]]:
        return self.data.setdefault("chats", {"Default": []})

    def load_messages(self, chat_name: str) -> List[Message]:
        return [
            Message(
                role=x.get("role", "user"),
                content=x.get("content", ""),
                timestamp=x.get("timestamp", now()),
                meta=x.get("meta", {})
            )
            for x in self.chats.get(chat_name, [])
        ]

    def save_messages(self, chat_name: str, messages: List[Message]) -> None:
        self.chats[chat_name] = [asdict(m) for m in messages]


# ==============================================================
# HTTP
# ==============================================================

class HttpClient:
    def __init__(self, logger: AppLogger, store: Store):
        self.logger = logger
        self.store = store

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        body: Any = None,
        timeout: Optional[int] = None,
        verify_ssl: bool = True,
        stream: bool = False,
    ):
        method = method.upper()
        headers = expand_env(headers or {})
        url = expand_env(url)
        timeout = timeout or int(self.store.settings.get("timeout", 120))

        request_id = uuid.uuid4().hex[:12]

        self.logger.write(
            "request",
            request_id=request_id,
            method=method,
            url=url,
            headers=headers,
            body_preview=body,
            stream=stream,
        )

        data = None
        if body is not None and method not in {"GET", "HEAD"}:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")

        req = urllib.request.Request(
            url=url,
            data=data,
            headers=headers,
            method=method,
        )

        context = None

        if url.startswith("https://") and not verify_ssl:
            import ssl
            context = ssl._create_unverified_context()

        try:
            return request_id, urllib.request.urlopen(
                req,
                timeout=timeout,
                context=context
            )
        except urllib.error.HTTPError as exc:
            payload = ""
            try:
                payload = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass

            self.logger.write(
                "http_error",
                request_id=request_id,
                status=exc.code,
                reason=str(exc.reason),
                response=payload[:10000],
            )
            raise RuntimeError(
                f"HTTP {exc.code}: {exc.reason}\n{payload[:3000]}"
            )
        except urllib.error.URLError as exc:
            self.logger.write(
                "network_error",
                request_id=request_id,
                error=str(exc.reason),
            )
            raise RuntimeError(f"Network error: {exc.reason}")
        except Exception as exc:
            self.logger.write(
                "request_error",
                request_id=request_id,
                error=repr(exc),
            )
            raise

    @staticmethod
    def read_json_response(response) -> Any:
        raw = response.read().decode("utf-8", errors="replace")
        if not raw.strip():
            return {}

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw_text": raw}

    @staticmethod
    def read_stream_lines(response):
        while True:
            line = response.readline()
            if not line:
                break
            yield line.decode("utf-8", errors="replace").rstrip("\r\n")


# ==============================================================
# PROVIDERS
# ==============================================================

class Provider:
    def __init__(
        self,
        name: str,
        cfg: Dict[str, Any],
        http: HttpClient
    ):
        self.name = name
        self.cfg = cfg
        self.http = http

    def send(
        self,
        messages: List[Message],
        stream: bool,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> ProviderResponse:
        raise NotImplementedError


class OpenAICompatibleProvider(Provider):
    def send(
        self,
        messages: List[Message],
        stream: bool,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> ProviderResponse:

        base = self.cfg.get("base_url", "").rstrip("/")
        endpoint = self.cfg.get("endpoint", "/chat/completions")
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint

        url = base + endpoint

        headers = expand_env(self.cfg.get("headers", {}))

        api_key = self.cfg.get("api_key", "")
        api_key_env = self.cfg.get("api_key_env", "")

        if api_key_env and not api_key:
            api_key = os.getenv(api_key_env, "")

        if api_key:
            headers.setdefault(
                "Authorization",
                f"Bearer {api_key}"
            )

        payload = {
            "model": self.cfg.get("model", ""),
            "messages": [
                {
                    "role": m.role,
                    "content": m.content
                }
                for m in messages
            ],
            "stream": bool(stream),
        }

        if self.cfg.get("temperature") is not None:
            payload["temperature"] = self.cfg["temperature"]

        if self.cfg.get("max_tokens") is not None:
            payload["max_tokens"] = self.cfg["max_tokens"]

        extra = self.cfg.get("extra_body", {})
        if isinstance(extra, dict):
            payload.update(expand_env(extra))

        request_id, response = self.http.request(
            "POST",
            url,
            headers=headers,
            body=payload,
            stream=stream,
            verify_ssl=bool(
                self.http.store.settings.get("verify_ssl", True)
            ),
        )

        if not stream:
            data = self.http.read_json_response(response)

            choice = (
                data.get("choices", [{}])[0]
                if isinstance(data, dict) and data.get("choices")
                else {}
            )

            msg = choice.get("message", {})
            text = msg.get("content") or ""

            self.http.logger.write(
                "response",
                request_id=request_id,
                provider=self.name,
                model=data.get("model", self.cfg.get("model", "")),
                usage=data.get("usage", {}),
            )

            return ProviderResponse(
                text=text,
                provider=self.name,
                model=data.get("model", self.cfg.get("model", "")),
                raw=data,
                usage=data.get("usage", {}) or {},
            )

        chunks: List[str] = []
        last_data: Any = {}

        # SSE-ish parser. Also tolerates raw JSON lines.
        for line in self.http.read_stream_lines(response):
            if not line:
                continue

            if line.startswith("data:"):
                line = line[5:].strip()

            if line == "[DONE]":
                break

            try:
                chunk = json.loads(line)
                last_data = chunk
            except Exception:
                continue

            choices = chunk.get("choices", [])
            if not choices:
                continue

            delta = choices[0].get("delta", {})
            token = delta.get("content") or ""

            if token:
                chunks.append(token)
                if on_token:
                    on_token(token)

        text = "".join(chunks)

        self.http.logger.write(
            "response",
            request_id=request_id,
            provider=self.name,
            model=last_data.get("model", self.cfg.get("model", "")),
            streamed=True,
        )

        return ProviderResponse(
            text=text,
            provider=self.name,
            model=last_data.get("model", self.cfg.get("model", "")),
            raw=last_data,
        )


class RestJsonProvider(Provider):
    def _render_template(self, obj: Any, context: Dict[str, Any]) -> Any:
        if isinstance(obj, dict):
            return {
                k: self._render_template(v, context)
                for k, v in obj.items()
            }

        if isinstance(obj, list):
            return [
                self._render_template(v, context)
                for v in obj
            ]

        if isinstance(obj, str):
            exact = re.fullmatch(r"\{\{([^}]+)\}\}", obj.strip())
            if exact:
                key = exact.group(1).strip()
                return context.get(key, "")

            result = obj
            for key, value in context.items():
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                result = result.replace(
                    "{{" + key + "}}",
                    str(value)
                )
            return result

        return obj

    def send(
        self,
        messages: List[Message],
        stream: bool,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> ProviderResponse:

        serial = [
            {
                "role": m.role,
                "content": m.content
            }
            for m in messages
        ]

        last_user = ""
        for m in reversed(messages):
            if m.role == "user":
                last_user = m.content
                break

        context = {
            "messages": serial,
            "last_user_message": last_user,
            "conversation_json": json.dumps(
                serial,
                ensure_ascii=False
            ),
            "timestamp": now(),
            "uuid": str(uuid.uuid4()),
        }

        body = self._render_template(
            self.cfg.get("body_template", {}),
            context
        )

        headers = expand_env(
            self.cfg.get("headers", {})
        )

        method = self.cfg.get("method", "POST").upper()
        url = expand_env(self.cfg.get("url", ""))

        request_id, response = self.http.request(
            method=method,
            url=url,
            headers=headers,
            body=body if method not in {"GET", "HEAD"} else None,
            stream=False,
            verify_ssl=bool(
                self.http.store.settings.get("verify_ssl", True)
            ),
        )

        data = self.http.read_json_response(response)

        path = self.cfg.get("response_path", "")
        extracted = nested_get(data, path, data)

        if isinstance(extracted, str):
            text = extracted
        elif extracted is None:
            text = ""
        else:
            text = json.dumps(
                extracted,
                ensure_ascii=False,
                indent=2
            )

        if on_token:
            on_token(text)

        self.http.logger.write(
            "response",
            request_id=request_id,
            provider=self.name,
            generic_json=True,
        )

        return ProviderResponse(
            text=text,
            provider=self.name,
            raw=data,
        )


class ProviderManager:
    def __init__(self, store: Store, logger: AppLogger):
        self.store = store
        self.logger = logger
        self.http = HttpClient(logger, store)

    def get(self, name: str) -> Provider:
        if name not in self.store.providers:
            raise KeyError(f"Unknown provider: {name}")

        cfg = self.store.providers[name]

        if not cfg.get("enabled", True):
            raise RuntimeError(f"Provider '{name}' is disabled.")

        ptype = cfg.get("type")

        if ptype == "openai_compatible":
            return OpenAICompatibleProvider(
                name, cfg, self.http
            )

        if ptype == "rest_json":
            return RestJsonProvider(
                name, cfg, self.http
            )

        raise ValueError(
            f"Unsupported provider type: {ptype}"
        )

    def test(self, name: str) -> ProviderResponse:
        provider = self.get(name)

        test_messages = [
            Message(
                role="system",
                content="You are a connectivity test. Reply with exactly: OK."
            ),
            Message(
                role="user",
                content="Connectivity test."
            )
        ]

        return provider.send(
            test_messages,
            stream=False
        )


# ==============================================================
# ROUTER
# ==============================================================

class Router:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def matches(text: str, rule: Dict[str, Any]) -> bool:
        low = text.lower()

        any_words = [
            str(x).lower()
            for x in rule.get("contains_any", [])
        ]

        all_words = [
            str(x).lower()
            for x in rule.get("contains_all", [])
        ]

        if any_words and not any(w in low for w in any_words):
            return False

        if all_words and not all(w in low for w in all_words):
            return False

        expression = rule.get("regex", "").strip()
        if expression:
            try:
                if not re.search(expression, text, re.I):
                    return False
            except re.error:
                return False

        return True

    def decide(
        self,
        text: str,
        forced_provider: Optional[str] = None
    ) -> Tuple[str, str]:

        if forced_provider and forced_provider != "auto":
            return forced_provider, "manual override"

        for rule in self.store.routing.get("rules", []):
            if self.matches(text, rule):
                return (
                    rule.get("provider", ""),
                    f"rule: {rule.get('name', 'unnamed')}"
                )

        default = self.store.routing.get("default", "")
        return default, "default"

    def candidates(self, selected: str) -> List[str]:
        result = [selected]
        for x in self.store.routing.get("fallbacks", []):
            if x and x not in result:
                result.append(x)
        return result


# ==============================================================
# LOCAL TOOLS
# ==============================================================

class Tools:
    """
    These are local helper functions.
    You can add more here without touching the rest of the program.
    """

    @staticmethod
    def calculator(expression: str) -> str:
        allowed = set(
            "0123456789+-*/().% "
        )

        if any(ch not in allowed for ch in expression):
            raise ValueError(
                "Only basic arithmetic characters are allowed."
            )

        result = eval(
            expression.replace("%", "/100"),
            {"__builtins__": {}},
            {}
        )
        return str(result)

    @staticmethod
    def current_time() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S %z")

    @staticmethod
    def make_uuid() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def sha256(text: str) -> str:
        return hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest()

    @staticmethod
    def base64_encode(text: str) -> str:
        return base64.b64encode(
            text.encode("utf-8")
        ).decode("ascii")

    @staticmethod
    def base64_decode(text: str) -> str:
        return base64.b64decode(
            text.encode("ascii")
        ).decode("utf-8", errors="replace")

    @staticmethod
    def url_encode(text: str) -> str:
        return urllib.parse.quote(text)

    @staticmethod
    def url_decode(text: str) -> str:
        return urllib.parse.unquote(text)

    @staticmethod
    def json_pretty(text: str) -> str:
        return pretty_json(json.loads(text))

    @staticmethod
    def http_get(url: str, timeout: int = 30) -> str:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode(
                "utf-8",
                errors="replace"
            )

    @staticmethod
    def run_command(command: str) -> str:
        """
        Deliberately available as a local POC tool.
        DO NOT wire arbitrary untrusted model output directly into this.
        """
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60
        )
        return (
            f"exit={result.returncode}\n\n"
            f"STDOUT:\n{result.stdout}\n\n"
            f"STDERR:\n{result.stderr}"
        )


# ==============================================================
# MAIN APPLICATION
# ==============================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Personal API Hub v2")
        self.geometry("1250x820")
        self.minsize(950, 650)

        self.store = Store()
        self.logger = AppLogger(LOG_FILE)
        self.providers = ProviderManager(
            self.store,
            self.logger
        )
        self.router = Router(self.store)

        self.current_chat = "Default"
        self.messages: List[Message] = (
            self.store.load_messages(self.current_chat)
        )

        self.busy = False
        self.chat_response_start = None

        self.protocol(
            "WM_DELETE_WINDOW",
            self.on_close
        )

        self._setup_style()
        self._build()
        self.refresh_all()

    # ----------------------------------------------------------
    # STYLE
    # ----------------------------------------------------------

    def _setup_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(
            "Title.TLabel",
            font=("TkDefaultFont", 16, "bold")
        )

        style.configure(
            "Heading.TLabel",
            font=("TkDefaultFont", 11, "bold")
        )

    # ----------------------------------------------------------
    # TOP-LEVEL UI
    # ----------------------------------------------------------

    def _build(self):
        header = ttk.Frame(self, padding=8)
        header.pack(fill="x")

        ttk.Label(
            header,
            text="PERSONAL API HUB",
            style="Title.TLabel"
        ).pack(side="left")

        ttk.Label(
            header,
            text="Local API router / chatbot / control panel"
        ).pack(side="left", padx=16)

        ttk.Button(
            header,
            text="Save Everything",
            command=self.save_everything
        ).pack(side="right")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(
            fill="both",
            expand=True,
            padx=8,
            pady=(0, 8)
        )

        self._build_chat_tab()
        self._build_api_tab()
        self._build_router_tab()
        self._build_logs_tab()
        self._build_tools_tab()
        self._build_settings_tab()

    # ----------------------------------------------------------
    # CHAT TAB
    # ----------------------------------------------------------

    def _build_chat_tab(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="Chat")

        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 8))

        ttk.Label(
            top,
            text="Chat:"
        ).pack(side="left")

        self.chat_var = tk.StringVar(value=self.current_chat)

        self.chat_combo = ttk.Combobox(
            top,
            textvariable=self.chat_var,
            state="readonly",
            width=24
        )
        self.chat_combo.pack(
            side="left",
            padx=6
        )
        self.chat_combo.bind(
            "<<ComboboxSelected>>",
            self.change_chat
        )

        ttk.Button(
            top,
            text="New",
            command=self.new_chat
        ).pack(side="left")

        ttk.Button(
            top,
            text="Rename",
            command=self.rename_chat
        ).pack(side="left", padx=4)

        ttk.Button(
            top,
            text="Delete",
            command=self.delete_chat
        ).pack(side="left")

        ttk.Separator(
            top,
            orient="vertical"
        ).pack(
            side="left",
            fill="y",
            padx=10
        )

        ttk.Label(
            top,
            text="Provider:"
        ).pack(side="left")

        self.provider_var = tk.StringVar(
            value="auto"
        )

        self.provider_combo = ttk.Combobox(
            top,
            textvariable=self.provider_var,
            state="readonly",
            width=24
        )

        self.provider_combo.pack(
            side="left",
            padx=6
        )

        ttk.Button(
            top,
            text="Test Route",
            command=self.test_current_route
        ).pack(side="left")

        self.chat_view = tk.Text(
            tab,
            wrap="word",
            state="disabled",
            font=("Consolas", 10),
            bg="#111318",
            fg="#e8e8e8",
            insertbackground="#ffffff",
        )
        self.chat_view.pack(
            fill="both",
            expand=True
        )

        self.chat_view.tag_configure(
            "user",
            foreground="#6bc5ff",
            font=("Consolas", 10, "bold")
        )
        self.chat_view.tag_configure(
            "assistant",
            foreground="#d5f5c7",
            font=("Consolas", 10, "bold")
        )
        self.chat_view.tag_configure(
            "system",
            foreground="#f0cf74",
            font=("Consolas", 10, "bold")
        )
        self.chat_view.tag_configure(
            "error",
            foreground="#ff7a7a",
            font=("Consolas", 10, "bold")
        )

        bottom = ttk.Frame(tab)
        bottom.pack(fill="x", pady=(8, 0))

        self.prompt_box = tk.Text(
            bottom,
            height=5,
            wrap="word",
            font=("Consolas", 10)
        )
        self.prompt_box.pack(
            side="left",
            fill="both",
            expand=True
        )

        right = ttk.Frame(bottom)
        right.pack(
            side="left",
            fill="y",
            padx=(8, 0)
        )

        self.send_button = ttk.Button(
            right,
            text="SEND",
            command=self.send_message
        )
        self.send_button.pack(
            fill="x",
            ipady=8
        )

        ttk.Button(
            right,
            text="Clear Chat",
            command=self.clear_current_chat
        ).pack(fill="x", pady=4)

        ttk.Button(
            right,
            text="Export",
            command=self.export_chat
        ).pack(fill="x")

        self.chat_status_var = tk.StringVar(
            value="Ready"
        )

        ttk.Label(
            right,
            textvariable=self.chat_status_var,
            wraplength=180
        ).pack(
            fill="x",
            pady=(8, 0)
        )

        self.prompt_box.bind(
            "<Control-Return>",
            lambda _e: self.send_message()
        )

    # ----------------------------------------------------------
    # API TAB
    # ----------------------------------------------------------

    def _build_api_tab(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="APIs")

        paned = ttk.PanedWindow(
            tab,
            orient="horizontal"
        )
        paned.pack(fill="both", expand=True)

        left = ttk.Frame(
            paned,
            padding=6
        )
        right = ttk.Frame(
            paned,
            padding=6
        )

        paned.add(left, weight=1)
        paned.add(right, weight=3)

        ttk.Label(
            left,
            text="Providers",
            style="Heading.TLabel"
        ).pack(anchor="w")

        self.provider_list = tk.Listbox(
            left,
            exportselection=False
        )
        self.provider_list.pack(
            fill="both",
            expand=True,
            pady=6
        )

        self.provider_list.bind(
            "<<ListboxSelect>>",
            self.load_selected_provider
        )

        buttons = ttk.Frame(left)
        buttons.pack(fill="x")

        ttk.Button(
            buttons,
            text="Add",
            command=self.add_provider
        ).pack(side="left", fill="x", expand=True)

        ttk.Button(
            buttons,
            text="Delete",
            command=self.delete_provider
        ).pack(side="left", fill="x", expand=True)

        ttk.Button(
            buttons,
            text="Duplicate",
            command=self.duplicate_provider
        ).pack(side="left", fill="x", expand=True)

        ttk.Button(
            left,
            text="Test Selected API",
            command=self.test_selected_provider
        ).pack(fill="x", pady=6)

        ttk.Button(
            left,
            text="Save API",
            command=self.save_provider_editor
        ).pack(fill="x")

        # Editor
        editor = ttk.Frame(right)
        editor.pack(fill="both", expand=True)

        row = 0

        ttk.Label(editor, text="Name").grid(
            row=row, column=0, sticky="w", pady=3
        )
        self.api_name_var = tk.StringVar()
        ttk.Entry(editor, textvariable=self.api_name_var).grid(
            row=row, column=1, sticky="ew", pady=3
        )
        row += 1

        ttk.Label(editor, text="Type").grid(
            row=row, column=0, sticky="w", pady=3
        )
        self.api_type_var = tk.StringVar(
            value="openai_compatible"
        )
        type_combo = ttk.Combobox(
            editor,
            textvariable=self.api_type_var,
            state="readonly",
            values=[
                "openai_compatible",
                "rest_json"
            ]
        )
        type_combo.grid(
            row=row, column=1, sticky="ew", pady=3
        )
        type_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: self.update_api_editor_help()
        )
        row += 1

        self.api_enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            editor,
            text="Enabled",
            variable=self.api_enabled_var
        ).grid(
            row=row, column=1, sticky="w", pady=3
        )
        row += 1

        ttk.Label(editor, text="Base URL / URL").grid(
            row=row, column=0, sticky="w", pady=3
        )
        self.api_url_var = tk.StringVar()
        ttk.Entry(editor, textvariable=self.api_url_var).grid(
            row=row, column=1, sticky="ew", pady=3
        )
        row += 1

        ttk.Label(editor, text="Endpoint").grid(
            row=row, column=0, sticky="w", pady=3
        )
        self.api_endpoint_var = tk.StringVar(
            value="/chat/completions"
        )
        ttk.Entry(
            editor,
            textvariable=self.api_endpoint_var
        ).grid(
            row=row, column=1, sticky="ew", pady=3
        )
        self.api_endpoint_label = editor.grid_slaves(row=row, column=0)[0]
        row += 1

        ttk.Label(editor, text="Method").grid(
            row=row, column=0, sticky="w", pady=3
        )
        self.api_method_var = tk.StringVar(value="POST")
        ttk.Combobox(
            editor,
            textvariable=self.api_method_var,
            values=["GET", "POST", "PUT", "PATCH", "DELETE"],
            state="readonly"
        ).grid(
            row=row, column=1, sticky="ew", pady=3
        )
        self.api_method_label = editor.grid_slaves(row=row, column=0)[0]
        row += 1

        ttk.Label(editor, text="Model").grid(
            row=row, column=0, sticky="w", pady=3
        )
        self.api_model_var = tk.StringVar()
        ttk.Entry(
            editor,
            textvariable=self.api_model_var
        ).grid(
            row=row, column=1, sticky="ew", pady=3
        )
        self.api_model_label = editor.grid_slaves(row=row, column=0)[0]
        row += 1

        ttk.Label(editor, text="API Key").grid(
            row=row, column=0, sticky="w", pady=3
        )
        self.api_key_var = tk.StringVar()
        ttk.Entry(
            editor,
            textvariable=self.api_key_var,
            show="*"
        ).grid(
            row=row, column=1, sticky="ew", pady=3
        )
        row += 1

        ttk.Label(editor, text="API Key Env Var").grid(
            row=row, column=0, sticky="w", pady=3
        )
        self.api_key_env_var = tk.StringVar()
        ttk.Entry(
            editor,
            textvariable=self.api_key_env_var
        ).grid(
            row=row, column=1, sticky="ew", pady=3
        )
        row += 1

        ttk.Label(editor, text="Temperature").grid(
            row=row, column=0, sticky="w", pady=3
        )
        self.api_temp_var = tk.StringVar(value="0.7")
        ttk.Entry(
            editor,
            textvariable=self.api_temp_var
        ).grid(
            row=row, column=1, sticky="ew", pady=3
        )
        self.api_temp_label = editor.grid_slaves(row=row, column=0)[0]
        row += 1

        ttk.Label(editor, text="Max Tokens").grid(
            row=row, column=0, sticky="w", pady=3
        )
        self.api_max_tokens_var = tk.StringVar(value="2048")
        ttk.Entry(
            editor,
            textvariable=self.api_max_tokens_var
        ).grid(
            row=row, column=1, sticky="ew", pady=3
        )
        self.api_max_tokens_label = editor.grid_slaves(row=row, column=0)[0]
        row += 1

        ttk.Label(
            editor,
            text="Headers JSON"
        ).grid(
            row=row, column=0, sticky="nw", pady=3
        )

        self.api_headers_text = tk.Text(
            editor,
            height=7,
            width=60,
            font=("Consolas", 9)
        )
        self.api_headers_text.grid(
            row=row, column=1, sticky="nsew", pady=3
        )
        row += 1

        ttk.Label(
            editor,
            text="Body Template / Extra JSON"
        ).grid(
            row=row, column=0, sticky="nw", pady=3
        )

        self.api_body_text = tk.Text(
            editor,
            height=10,
            width=60,
            font=("Consolas", 9)
        )
        self.api_body_text.grid(
            row=row, column=1, sticky="nsew", pady=3
        )
        row += 1

        ttk.Label(
            editor,
            text="Response Path"
        ).grid(
            row=row, column=0, sticky="w", pady=3
        )
        self.api_response_path_var = tk.StringVar()
        ttk.Entry(
            editor,
            textvariable=self.api_response_path_var
        ).grid(
            row=row, column=1, sticky="ew", pady=3
        )
        self.api_response_path_label = editor.grid_slaves(row=row, column=0)[0]
        row += 1

        editor.columnconfigure(1, weight=1)
        editor.rowconfigure(row - 3, weight=1)
        editor.rowconfigure(row - 2, weight=1)

        self.api_help = tk.StringVar()
        ttk.Label(
            editor,
            textvariable=self.api_help,
            wraplength=650,
            justify="left"
        ).grid(
            row=row,
            column=0,
            columnspan=2,
            sticky="w",
            pady=8
        )

    # ----------------------------------------------------------
    # ROUTER TAB
    # ----------------------------------------------------------

    def _build_router_tab(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="Router")

        upper = ttk.Frame(tab)
        upper.pack(fill="x")

        ttk.Label(
            upper,
            text="Default Provider"
        ).grid(row=0, column=0, sticky="w")

        self.route_default_var = tk.StringVar()
        self.route_default_combo = ttk.Combobox(
            upper,
            textvariable=self.route_default_var,
            state="readonly",
            width=30
        )
        self.route_default_combo.grid(
            row=0, column=1, sticky="w", padx=6
        )

        ttk.Label(
            upper,
            text="Fallbacks (comma-separated)"
        ).grid(row=1, column=0, sticky="w", pady=6)

        self.route_fallback_var = tk.StringVar()
        ttk.Entry(
            upper,
            textvariable=self.route_fallback_var,
            width=60
        ).grid(
            row=1, column=1, sticky="w", padx=6
        )

        ttk.Button(
            upper,
            text="Save Routing",
            command=self.save_routing
        ).grid(
            row=0, column=2, rowspan=2, padx=10
        )

        ttk.Separator(
            tab,
            orient="horizontal"
        ).pack(fill="x", pady=10)

        rule_buttons = ttk.Frame(tab)
        rule_buttons.pack(fill="x")

        ttk.Button(
            rule_buttons,
            text="Add Rule",
            command=self.add_rule
        ).pack(side="left")

        ttk.Button(
            rule_buttons,
            text="Delete Rule",
            command=self.delete_rule
        ).pack(side="left", padx=5)

        ttk.Button(
            rule_buttons,
            text="Save Rules",
            command=self.save_rules
        ).pack(side="left")

        self.rule_list = tk.Listbox(
            tab,
            height=10,
            exportselection=False
        )
        self.rule_list.pack(
            fill="both",
            expand=True,
            pady=8
        )
        self.rule_list.bind(
            "<<ListboxSelect>>",
            self.load_selected_rule
        )

        rule_editor = ttk.LabelFrame(
            tab,
            text="Selected Rule",
            padding=8
        )
        rule_editor.pack(fill="x")

        self.rule_name_var = tk.StringVar()
        self.rule_provider_var = tk.StringVar()
        self.rule_any_var = tk.StringVar()
        self.rule_all_var = tk.StringVar()
        self.rule_regex_var = tk.StringVar()

        fields = [
            ("Name", self.rule_name_var),
            ("Provider", self.rule_provider_var),
            ("Contains ANY", self.rule_any_var),
            ("Contains ALL", self.rule_all_var),
            ("Regex", self.rule_regex_var),
        ]

        for i, (label, var) in enumerate(fields):
            ttk.Label(
                rule_editor,
                text=label
            ).grid(
                row=i, column=0, sticky="w", pady=3
            )
            ttk.Entry(
                rule_editor,
                textvariable=var
            ).grid(
                row=i, column=1, sticky="ew", pady=3
            )

        rule_editor.columnconfigure(1, weight=1)

        ttk.Label(
            tab,
            text="Routing test"
        ).pack(
            anchor="w",
            pady=(10, 3)
        )

        route_test_frame = ttk.Frame(tab)
        route_test_frame.pack(fill="x")

        self.route_test_var = tk.StringVar()
        ttk.Entry(
            route_test_frame,
            textvariable=self.route_test_var
        ).pack(
            side="left",
            fill="x",
            expand=True
        )

        ttk.Button(
            route_test_frame,
            text="Test",
            command=self.test_route_text
        ).pack(
            side="left",
            padx=6
        )

        self.route_test_result_var = tk.StringVar()
        ttk.Label(
            tab,
            textvariable=self.route_test_result_var,
            wraplength=900
        ).pack(
            anchor="w",
            pady=6
        )

    # ----------------------------------------------------------
    # LOGS TAB
    # ----------------------------------------------------------

    def _build_logs_tab(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="Logs")

        controls = ttk.Frame(tab)
        controls.pack(fill="x")

        ttk.Label(
            controls,
            text="Search"
        ).pack(side="left")

        self.log_search_var = tk.StringVar()
        ttk.Entry(
            controls,
            textvariable=self.log_search_var
        ).pack(
            side="left",
            fill="x",
            expand=True,
            padx=6
        )

        self.log_event_var = tk.StringVar(value="ALL")
        ttk.Combobox(
            controls,
            textvariable=self.log_event_var,
            state="readonly",
            width=18,
            values=["ALL"]
        ).pack(side="left", padx=6)

        ttk.Button(
            controls,
            text="Refresh",
            command=self.refresh_logs
        ).pack(side="left")

        ttk.Button(
            controls,
            text="Export",
            command=self.export_logs
        ).pack(side="left", padx=4)

        ttk.Button(
            controls,
            text="Clear",
            command=self.clear_logs
        ).pack(side="left")

        paned = ttk.PanedWindow(
            tab,
            orient="vertical"
        )
        paned.pack(
            fill="both",
            expand=True,
            pady=(8, 0)
        )

        top = ttk.Frame(paned)
        bottom = ttk.Frame(paned)

        paned.add(top, weight=2)
        paned.add(bottom, weight=1)

        columns = (
            "timestamp",
            "event",
            "provider",
            "request_id",
        )

        self.log_tree = ttk.Treeview(
            top,
            columns=columns,
            show="headings"
        )

        for col, width in [
            ("timestamp", 160),
            ("event", 160),
            ("provider", 160),
            ("request_id", 120),
        ]:
            self.log_tree.heading(col, text=col.upper())
            self.log_tree.column(col, width=width)

        self.log_tree.pack(
            fill="both",
            expand=True
        )

        self.log_tree.bind(
            "<<TreeviewSelect>>",
            self.show_selected_log
        )

        self.log_detail = tk.Text(
            bottom,
            wrap="word",
            font=("Consolas", 9)
        )
        self.log_detail.pack(
            fill="both",
            expand=True
        )

    # ----------------------------------------------------------
    # TOOLS TAB
    # ----------------------------------------------------------

    def _build_tools_tab(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="Tools")

        ttk.Label(
            tab,
            text="Built-in local utility tools",
            style="Heading.TLabel"
        ).pack(anchor="w")

        self.tool_text = tk.Text(
            tab,
            wrap="word",
            font=("Consolas", 10)
        )
        self.tool_text.pack(
            fill="both",
            expand=True,
            pady=8
        )

        ttk.Button(
            tab,
            text="Open Tool Playground",
            command=self.open_tool_playground
        ).pack(anchor="w")

        ttk.Label(
            tab,
            text=(
                "The tool functions live in the Tools class in this same file. "
                "Add your own API calls, filesystem helpers, database calls, "
                "scripts, web requests, etc. there."
            ),
            wraplength=900,
            justify="left"
        ).pack(
            anchor="w",
            pady=8
        )

    # ----------------------------------------------------------
    # SETTINGS TAB
    # ----------------------------------------------------------

    def _build_settings_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Settings")

        self.settings_prompt = tk.Text(
            tab,
            height=8,
            wrap="word"
        )
        self.settings_prompt.pack(
            fill="x",
            pady=(0, 10)
        )

        grid = ttk.Frame(tab)
        grid.pack(fill="x")

        self.settings_history_var = tk.StringVar()
        self.settings_timeout_var = tk.StringVar()
        self.settings_verify_var = tk.BooleanVar()
        self.settings_stream_var = tk.BooleanVar()
        self.settings_autosave_var = tk.BooleanVar()

        rows = [
            (
                "History messages",
                self.settings_history_var
            ),
            (
                "HTTP timeout (seconds)",
                self.settings_timeout_var
            ),
        ]

        for i, (label, var) in enumerate(rows):
            ttk.Label(
                grid,
                text=label
            ).grid(
                row=i, column=0, sticky="w", pady=4
            )
            ttk.Entry(
                grid,
                textvariable=var,
                width=20
            ).grid(
                row=i, column=1, sticky="w", padx=8
            )

        ttk.Checkbutton(
            grid,
            text="Verify SSL",
            variable=self.settings_verify_var
        ).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=4
        )

        ttk.Checkbutton(
            grid,
            text="Stream OpenAI-compatible responses",
            variable=self.settings_stream_var
        ).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=4
        )

        ttk.Checkbutton(
            grid,
            text="Autosave",
            variable=self.settings_autosave_var
        ).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=4
        )

        ttk.Button(
            tab,
            text="Save Settings",
            command=self.save_settings
        ).pack(
            anchor="w",
            pady=10
        )

        ttk.Label(
            tab,
            text=(
                f"Data file: {DATA_FILE}\n"
                f"Log file: {LOG_FILE}"
            ),
            wraplength=1000,
            justify="left"
        ).pack(
            anchor="w",
            pady=10
        )

    # ----------------------------------------------------------
    # REFRESH
    # ----------------------------------------------------------

    def refresh_all(self):
        self.refresh_provider_lists()
        self.refresh_provider_editor_list()
        self.refresh_routing()
        self.refresh_logs()
        self.refresh_tools()
        self.refresh_settings()
        self.render_chat()
        self.update_api_editor_help()

    def refresh_provider_lists(self):
        names = list(self.store.providers.keys())

        self.provider_combo["values"] = [
            "auto"
        ] + names

        if self.provider_var.get() not in ["auto"] + names:
            self.provider_var.set("auto")

        self.route_default_combo["values"] = names

    def refresh_provider_editor_list(self):
        current = self.api_name_var.get()

        self.provider_list.delete(
            0,
            "end"
        )

        for name, cfg in self.store.providers.items():
            marker = "●" if cfg.get("enabled", True) else "○"
            self.provider_list.insert(
                "end",
                f"{marker} {name}"
            )

        names = list(self.store.providers.keys())

        if current in names:
            idx = names.index(current)
            self.provider_list.selection_clear(0, "end")
            self.provider_list.selection_set(idx)
            self.provider_list.see(idx)

    def refresh_routing(self):
        names = list(self.store.providers.keys())

        self.route_default_combo["values"] = names

        default = self.store.routing.get("default", "")
        self.route_default_var.set(default)

        self.route_fallback_var.set(
            ", ".join(
                self.store.routing.get("fallbacks", [])
            )
        )

        self.rule_list.delete(
            0,
            "end"
        )

        for rule in self.store.routing.get("rules", []):
            self.rule_list.insert(
                "end",
                rule.get("name", "unnamed")
            )

    def refresh_logs(self):
        records = self.logger.read_all()

        event_names = ["ALL"] + sorted(
            {
                x.get("event", "unknown")
                for x in records
            }
        )

        combo = None

        # Find the log event combobox without retaining another reference.
        for child in self.notebook.winfo_children():
            # no-op; the explicit values update is done using widget search below
            pass

        # The widget was not assigned to an instance in the initial version;
        # locate it among the controls for simplicity.
        log_search = self.log_search_var.get().lower()
        selected_event = self.log_event_var.get()

        self.log_tree.delete(
            *self.log_tree.get_children()
        )

        for idx, item in enumerate(reversed(records)):
            if selected_event != "ALL" and item.get("event") != selected_event:
                continue

            blob = pretty_json(item).lower()

            if log_search and log_search not in blob:
                continue

            iid = str(idx)

            self.log_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    item.get("timestamp", ""),
                    item.get("event", ""),
                    item.get("provider", ""),
                    item.get("request_id", ""),
                )
            )

        self._all_visible_logs = list(reversed(records))

        self.tool_log_events = event_names

    def refresh_tools(self):
        text = (
            "calculator(expression)\n"
            "  Basic arithmetic.\n\n"
            "current_time()\n"
            "  Local machine time.\n\n"
            "make_uuid()\n"
            "  Generate UUID4.\n\n"
            "sha256(text)\n"
            "  SHA-256 hash.\n\n"
            "base64_encode(text)\n"
            "base64_decode(text)\n\n"
            "url_encode(text)\n"
            "url_decode(text)\n\n"
            "json_pretty(text)\n"
            "  Pretty-print JSON.\n\n"
            "http_get(url)\n"
            "  Simple HTTP GET.\n\n"
            "run_command(command)\n"
            "  Local shell command execution; intentionally exposed only "
            "as a local POC utility.\n"
        )

        self.tool_text.delete(
            "1.0",
            "end"
        )
        self.tool_text.insert(
            "1.0",
            text
        )

    def refresh_settings(self):
        settings = self.store.settings

        self.settings_prompt.delete(
            "1.0",
            "end"
        )
        self.settings_prompt.insert(
            "1.0",
            settings.get("system_prompt", "")
        )

        self.settings_history_var.set(
            str(settings.get("history_limit", 40))
        )
        self.settings_timeout_var.set(
            str(settings.get("timeout", 120))
        )

        self.settings_verify_var.set(
            bool(settings.get("verify_ssl", True))
        )

        self.settings_stream_var.set(
            bool(settings.get("stream", True))
        )

        self.settings_autosave_var.set(
            bool(settings.get("autosave", True))
        )

    # ----------------------------------------------------------
    # CHAT
    # ----------------------------------------------------------

    def render_chat(self):
        self.chat_view.configure(
            state="normal"
        )
        self.chat_view.delete(
            "1.0",
            "end"
        )

        for m in self.messages:
            tag = (
                "user"
                if m.role == "user"
                else "assistant"
                if m.role == "assistant"
                else "system"
            )

            label = (
                "YOU"
                if m.role == "user"
                else "AI"
                if m.role == "assistant"
                else "SYSTEM"
            )

            self.chat_view.insert(
                "end",
                f"{label}  {m.timestamp}\n",
                tag
            )
            self.chat_view.insert(
                "end",
                m.content + "\n\n"
            )

        self.chat_view.configure(
            state="disabled"
        )
        self.chat_view.see("end")

    def append_chat(self, role: str, content: str):
        tag = (
            "user"
            if role == "user"
            else "assistant"
            if role == "assistant"
            else "error"
        )

        label = (
            "YOU"
            if role == "user"
            else "AI"
            if role == "assistant"
            else "ERROR"
        )

        self.chat_view.configure(
            state="normal"
        )
        self.chat_view.insert(
            "end",
            f"{label}  {now()}\n",
            tag
        )
        self.chat_view.insert(
            "end",
            content + "\n\n"
        )
        self.chat_view.configure(
            state="disabled"
        )
        self.chat_view.see("end")

    def send_message(self):
        if self.busy:
            return

        text = self.prompt_box.get(
            "1.0",
            "end"
        ).strip()

        if not text:
            return

        # Local slash commands.
        if self.handle_command(text):
            self.prompt_box.delete("1.0", "end")
            return

        self.prompt_box.delete(
            "1.0",
            "end"
        )

        self.messages.append(
            Message(
                role="user",
                content=text
            )
        )

        self.trim_history()
        self.append_chat(
            "user",
            text
        )

        self.busy = True
        self.send_button.configure(
            state="disabled"
        )
        self.chat_status_var.set(
            "Routing..."
        )

        forced = self.provider_var.get()
        if forced == "auto":
            forced = None

        threading.Thread(
            target=self._send_worker,
            args=(text, forced),
            daemon=True
        ).start()

    def _send_worker(
        self,
        text: str,
        forced: Optional[str]
    ):
        try:
            provider, reason = self.router.decide(
                text,
                forced
            )

            candidates = self.router.candidates(
                provider
            )

            self.logger.write(
                "route",
                message=text,
                selected=provider,
                reason=reason,
                candidates=candidates
            )

            self.after(
                0,
                lambda: self.chat_status_var.set(
                    f"Route: {provider} ({reason})"
                )
            )

            # Context = system + conversation.
            outgoing = [
                Message(
                    role="system",
                    content=self.store.settings.get(
                        "system_prompt",
                        ""
                    )
                )
            ]

            outgoing.extend(self.messages)

            use_stream = bool(
                self.store.settings.get(
                    "stream",
                    True
                )
            )

            last_exc = None

            for candidate in candidates:
                try:
                    provider_obj = self.providers.get(
                        candidate
                    )

                    response = provider_obj.send(
                        outgoing,
                        stream=use_stream
                        and isinstance(
                            provider_obj,
                            OpenAICompatibleProvider
                        ),
                        on_token=lambda token: self.after(
                            0,
                            self._insert_stream_token,
                            token
                        )
                    )

                    self.after(
                        0,
                        self._finish_response,
                        response
                    )
                    return

                except Exception as exc:
                    last_exc = exc
                    self.logger.write(
                        "provider_failed",
                        provider=candidate,
                        error=repr(exc)
                    )

            raise RuntimeError(
                f"All routes failed. Last error: {last_exc}"
            )

        except Exception as exc:
            self.after(
                0,
                self._handle_send_error,
                str(exc)
            )

    def _insert_stream_token(self, token: str):
        if self.chat_response_start is None:
            self.chat_view.configure(
                state="normal"
            )
            self.chat_view.insert(
                "end",
                f"AI  {now()}\n",
                "assistant"
            )
            self.chat_response_start = (
                self.chat_view.index("end-1c")
            )
            self.chat_view.configure(
                state="disabled"
            )

        self.chat_view.configure(
            state="normal"
        )
        self.chat_view.insert(
            "end",
            token
        )
        self.chat_view.configure(
            state="disabled"
        )
        self.chat_view.see("end")

    def _finish_response(
        self,
        response: ProviderResponse
    ):
        self.chat_response_start = None

        # When streaming is active, text was already rendered.
        streaming = bool(
            self.store.settings.get("stream", True)
        ) and response.provider in self.store.providers \
            and self.store.providers[response.provider].get(
                "type"
            ) == "openai_compatible"

        if not streaming:
            self.append_chat(
                "assistant",
                response.text
            )

        self.messages.append(
            Message(
                role="assistant",
                content=response.text,
                meta={
                    "provider": response.provider,
                    "model": response.model,
                    "usage": response.usage
                }
            )
        )

        self.trim_history()
        self.persist_current_chat()

        self.busy = False
        self.send_button.configure(
            state="normal"
        )

        self.chat_status_var.set(
            f"Done: {response.provider}"
            + (
                f" / {response.model}"
                if response.model
                else ""
            )
        )

    def _handle_send_error(self, text: str):
        self.busy = False
        self.send_button.configure(
            state="normal"
        )
        self.append_chat(
            "error",
            text
        )
        self.chat_status_var.set(
            "Error"
        )

    def trim_history(self):
        limit = int(
            self.store.settings.get(
                "history_limit",
                40
            )
        )

        if len(self.messages) > limit:
            self.messages = self.messages[-limit:]

    def persist_current_chat(self):
        self.store.save_messages(
            self.current_chat,
            self.messages
        )

        if self.store.settings.get(
            "autosave",
            True
        ):
            self.store.save()

    def clear_current_chat(self):
        self.messages = []
        self.persist_current_chat()
        self.render_chat()

    def new_chat(self):
        name = simpledialog.askstring(
            "New Chat",
            "Chat name:",
            parent=self
        )

        if not name:
            return

        if name in self.store.chats:
            messagebox.showerror(
                "Exists",
                "A chat with that name already exists."
            )
            return

        self.store.chats[name] = []
        self.store.save()
        self.current_chat = name
        self.chat_var.set(name)
        self.messages = []
        self.refresh_chat_combo()

    def rename_chat(self):
        old = self.current_chat

        new = simpledialog.askstring(
            "Rename Chat",
            "New name:",
            initialvalue=old,
            parent=self
        )

        if not new or new == old:
            return

        if new in self.store.chats:
            messagebox.showerror(
                "Exists",
                "A chat with that name already exists."
            )
            return

        self.store.chats[new] = self.store.chats.pop(old)
        self.current_chat = new
        self.chat_var.set(new)
        self.store.save()
        self.refresh_chat_combo()

    def delete_chat(self):
        if len(self.store.chats) <= 1:
            messagebox.showinfo(
                "Delete Chat",
                "Keep at least one chat."
            )
            return

        if not messagebox.askyesno(
            "Delete Chat",
            f"Delete '{self.current_chat}'?"
        ):
            return

        del self.store.chats[
            self.current_chat
        ]

        self.current_chat = next(
            iter(self.store.chats)
        )

        self.chat_var.set(
            self.current_chat
        )

        self.messages = self.store.load_messages(
            self.current_chat
        )

        self.store.save()
        self.refresh_chat_combo()

    def refresh_chat_combo(self):
        self.chat_combo["values"] = list(
            self.store.chats.keys()
        )
        self.chat_combo.set(
            self.current_chat
        )
        self.render_chat()

    def change_chat(self, _event=None):
        self.current_chat = (
            self.chat_var.get()
        )
        self.messages = self.store.load_messages(
            self.current_chat
        )
        self.render_chat()

    def export_chat(self):
        path = filedialog.asksaveasfilename(
            title="Export Chat",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")]
        )
        if not path:
            return

        save_json(
            Path(path),
            {
                "chat": self.current_chat,
                "messages": [
                    asdict(m)
                    for m in self.messages
                ]
            }
        )

    # ----------------------------------------------------------
    # COMMANDS
    # ----------------------------------------------------------

    def handle_command(self, text: str) -> bool:
        if not text.startswith("/"):
            return False

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/help":
            self.append_chat(
                "system",
                (
                    "/help\n"
                    "/clear\n"
                    "/provider NAME\n"
                    "/route TEXT\n"
                    "/apis\n"
                    "/logs"
                )
            )
            return True

        if cmd == "/clear":
            self.clear_current_chat()
            return True

        if cmd == "/provider":
            names = ["auto"] + list(
                self.store.providers
            )
            if arg in names:
                self.provider_var.set(arg)
                self.append_chat(
                    "system",
                    f"Provider set to {arg}"
                )
            else:
                self.append_chat(
                    "system",
                    "Providers: " + ", ".join(names)
                )
            return True

        if cmd == "/route":
            provider, reason = self.router.decide(
                arg
            )
            self.append_chat(
                "system",
                f"{provider} ({reason})"
            )
            return True

        if cmd == "/apis":
            self.notebook.select(
                self.api_tab()
            )
            return True

        if cmd == "/logs":
            self.notebook.select(
                self.logs_tab()
            )
            return True

        return False

    def api_tab(self):
        return self.notebook.tabs().index(
            self.notebook.tabs()[
                1
            ]
        )

    def logs_tab(self):
        return self.notebook.tabs().index(
            self.notebook.tabs()[
                3
            ]
        )

    # ----------------------------------------------------------
    # API EDITOR
    # ----------------------------------------------------------

    def selected_provider_name(self) -> Optional[str]:
        selection = self.provider_list.curselection()
        if not selection:
            return None

        raw = self.provider_list.get(
            selection[0]
        )

        return raw[2:].strip()

    def load_selected_provider(self, _event=None):
        name = self.selected_provider_name()
        if not name:
            return

        cfg = self.store.providers[name]

        self.api_name_var.set(name)
        self.api_type_var.set(
            cfg.get("type", "openai_compatible")
        )
        self.api_enabled_var.set(
            bool(cfg.get("enabled", True))
        )

        if cfg.get("type") == "rest_json":
            self.api_url_var.set(
                cfg.get("url", "")
            )
        else:
            self.api_url_var.set(
                cfg.get("base_url", "")
            )

        self.api_endpoint_var.set(
            cfg.get(
                "endpoint",
                "/chat/completions"
            )
        )

        self.api_method_var.set(
            cfg.get("method", "POST")
        )

        self.api_model_var.set(
            cfg.get("model", "")
        )

        self.api_key_var.set(
            cfg.get("api_key", "")
        )

        self.api_key_env_var.set(
            cfg.get("api_key_env", "")
        )

        self.api_temp_var.set(
            str(cfg.get("temperature", 0.7))
        )

        self.api_max_tokens_var.set(
            str(cfg.get("max_tokens", 2048))
        )

        self.api_headers_text.delete(
            "1.0",
            "end"
        )
        self.api_headers_text.insert(
            "1.0",
            pretty_json(
                cfg.get("headers", {})
            )
        )

        body = (
            cfg.get("body_template", {})
            if cfg.get("type") == "rest_json"
            else cfg.get("extra_body", {})
        )

        self.api_body_text.delete(
            "1.0",
            "end"
        )
        self.api_body_text.insert(
            "1.0",
            pretty_json(body)
        )

        self.api_response_path_var.set(
            cfg.get("response_path", "")
        )

        self.update_api_editor_help()

    def add_provider(self):
        name = simpledialog.askstring(
            "Add API",
            "Provider name:",
            parent=self
        )

        if not name:
            return

        name = name.strip()

        if not re.match(
            r"^[A-Za-z0-9_.-]+$",
            name
        ):
            messagebox.showerror(
                "Invalid Name",
                "Use letters, digits, _, -, or ."
            )
            return

        if name in self.store.providers:
            messagebox.showerror(
                "Exists",
                "Provider already exists."
            )
            return

        self.store.providers[name] = {
            "type": "openai_compatible",
            "enabled": True,
            "base_url": "",
            "endpoint": "/chat/completions",
            "model": "",
            "api_key": "",
            "api_key_env": "",
            "headers": {
                "Content-Type": "application/json"
            },
            "temperature": 0.7,
            "max_tokens": 2048,
            "extra_body": {}
        }

        self.store.save()
        self.refresh_all()

        names = list(
            self.store.providers.keys()
        )
        idx = names.index(name)

        self.provider_list.selection_clear(
            0, "end"
        )
        self.provider_list.selection_set(
            idx
        )
        self.provider_list.event_generate(
            "<<ListboxSelect>>"
        )

    def delete_provider(self):
        name = self.selected_provider_name()

        if not name:
            return

        if not messagebox.askyesno(
            "Delete API",
            f"Delete provider '{name}'?"
        ):
            return

        self.store.providers.pop(
            name,
            None
        )

        if self.store.routing.get("default") == name:
            self.store.routing["default"] = ""

        self.store.routing["fallbacks"] = [
            x for x in self.store.routing.get(
                "fallbacks",
                []
            )
            if x != name
        ]

        self.store.routing["rules"] = [
            r for r in self.store.routing.get(
                "rules",
                []
            )
            if r.get("provider") != name
        ]

        self.store.save()
        self.refresh_all()

    def duplicate_provider(self):
        name = self.selected_provider_name()
        if not name:
            return

        new_name = simpledialog.askstring(
            "Duplicate API",
            "New provider name:",
            initialvalue=name + "_copy",
            parent=self
        )

        if not new_name:
            return

        if new_name in self.store.providers:
            messagebox.showerror(
                "Exists",
                "Provider already exists."
            )
            return

        self.store.providers[new_name] = deep_copy(
            self.store.providers[name]
        )

        self.store.save()
        self.refresh_all()

    def save_provider_editor(self):
        name = self.api_name_var.get().strip()
        ptype = self.api_type_var.get()

        if not name:
            messagebox.showerror(
                "Missing name",
                "Provider name is required."
            )
            return

        try:
            headers = json.loads(
                self.api_headers_text.get(
                    "1.0",
                    "end"
                ).strip() or "{}"
            )

            body = json.loads(
                self.api_body_text.get(
                    "1.0",
                    "end"
                ).strip() or "{}"
            )
        except Exception as exc:
            messagebox.showerror(
                "Invalid JSON",
                str(exc)
            )
            return

        old_name = self.selected_provider_name()

        cfg: Dict[str, Any] = {
            "type": ptype,
            "enabled": self.api_enabled_var.get(),
            "api_key": self.api_key_var.get(),
            "api_key_env": self.api_key_env_var.get(),
            "headers": headers,
        }

        if ptype == "openai_compatible":
            cfg.update({
                "base_url": self.api_url_var.get(),
                "endpoint": self.api_endpoint_var.get(),
                "model": self.api_model_var.get(),
                "temperature": float(
                    self.api_temp_var.get()
                ),
                "max_tokens": int(
                    self.api_max_tokens_var.get()
                ),
                "extra_body": body,
            })
        else:
            cfg.update({
                "url": self.api_url_var.get(),
                "method": self.api_method_var.get(),
                "body_template": body,
                "response_path": self.api_response_path_var.get(),
            })

        if (
            old_name
            and old_name != name
            and old_name in self.store.providers
        ):
            self.store.providers.pop(
                old_name
            )

            # Update routing references after rename.
            if self.store.routing.get("default") == old_name:
                self.store.routing["default"] = name

            self.store.routing["fallbacks"] = [
                name if x == old_name else x
                for x in self.store.routing.get(
                    "fallbacks",
                    []
                )
            ]

            for rule in self.store.routing.get("rules", []):
                if rule.get("provider") == old_name:
                    rule["provider"] = name

        self.store.providers[name] = cfg
        self.store.save()
        self.refresh_all()

        messagebox.showinfo(
            "Saved",
            f"Provider '{name}' saved."
        )

    def update_api_editor_help(self):
        ptype = self.api_type_var.get()

        if ptype == "openai_compatible":
            self.api_endpoint_label.grid()
            self.api_model_label.grid()
            self.api_temp_label.grid()
            self.api_max_tokens_label.grid()
            self.api_response_path_label.grid_remove()
            self.api_method_label.grid_remove()

            self.api_help.set(
                "OpenAI-compatible example:\n"
                "URL = https://host/v1\n"
                "Endpoint = /chat/completions\n"
                "The program sends JSON with model, messages, stream, "
                "temperature and max_tokens.\n\n"
                "API Key can be entered directly, or leave it blank and "
                "set an API Key Env Var such as OPENAI_API_KEY."
            )
        else:
            self.api_endpoint_label.grid_remove()
            self.api_model_label.grid_remove()
            self.api_temp_label.grid_remove()
            self.api_max_tokens_label.grid_remove()
            self.api_response_path_label.grid()
            self.api_method_label.grid()

            self.api_help.set(
                "Generic JSON mode:\n"
                "Body template placeholders:\n"
                "{{messages}}\n"
                "{{last_user_message}}\n"
                "{{conversation_json}}\n"
                "{{timestamp}}\n"
                "{{uuid}}\n\n"
                "Response Path is a dot path such as "
                "data.reply or choices.0.text."
            )

    def test_selected_provider(self):
        name = self.selected_provider_name()

        if not name:
            return

        self._test_provider_async(name)

    def _test_provider_async(self, name: str):
        def worker():
            try:
                result = self.providers.test(name)
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        "API Test",
                        f"Provider: {name}\n\n"
                        f"Response:\n{result.text[:5000]}"
                    )
                )
            except Exception as exc:
                self.after(
                    0,
                    lambda: messagebox.showerror(
                        "API Test Failed",
                        str(exc)
                    )
                )

        threading.Thread(
            target=worker,
            daemon=True
        ).start()

    # ----------------------------------------------------------
    # ROUTER
    # ----------------------------------------------------------

    def save_routing(self):
        self.store.routing["default"] = (
            self.route_default_var.get()
        )

        raw = self.route_fallback_var.get().strip()

        self.store.routing["fallbacks"] = [
            x.strip()
            for x in raw.split(",")
            if x.strip()
        ]

        self.store.save()
        self.refresh_routing()

    def add_rule(self):
        name = simpledialog.askstring(
            "Add Rule",
            "Rule name:",
            parent=self
        )

        if not name:
            return

        self.store.routing.setdefault(
            "rules",
            []
        ).append({
            "name": name,
            "provider": "",
            "contains_any": [],
            "contains_all": [],
            "regex": ""
        })

        self.store.save()
        self.refresh_routing()

        idx = len(
            self.store.routing["rules"]
        ) - 1

        self.rule_list.selection_clear(
            0, "end"
        )
        self.rule_list.selection_set(idx)
        self.rule_list.event_generate(
            "<<ListboxSelect>>"
        )

    def delete_rule(self):
        selection = self.rule_list.curselection()

        if not selection:
            return

        idx = selection[0]

        self.store.routing["rules"].pop(
            idx
        )

        self.store.save()
        self.refresh_routing()

    def load_selected_rule(self, _event=None):
        selection = self.rule_list.curselection()

        if not selection:
            return

        idx = selection[0]
        rules = self.store.routing.get(
            "rules",
            []
        )

        if idx >= len(rules):
            return

        rule = rules[idx]

        self.rule_name_var.set(
            rule.get("name", "")
        )
        self.rule_provider_var.set(
            rule.get("provider", "")
        )
        self.rule_any_var.set(
            ", ".join(
                rule.get("contains_any", [])
            )
        )
        self.rule_all_var.set(
            ", ".join(
                rule.get("contains_all", [])
            )
        )
        self.rule_regex_var.set(
            rule.get("regex", "")
        )

    def save_rules(self):
        selection = self.rule_list.curselection()

        if not selection:
            messagebox.showinfo(
                "Rules",
                "Select a rule first."
            )
            return

        idx = selection[0]

        def split_csv(value):
            return [
                x.strip()
                for x in value.split(",")
                if x.strip()
            ]

        self.store.routing["rules"][idx] = {
            "name": self.rule_name_var.get().strip(),
            "provider": self.rule_provider_var.get().strip(),
            "contains_any": split_csv(
                self.rule_any_var.get()
            ),
            "contains_all": split_csv(
                self.rule_all_var.get()
            ),
            "regex": self.rule_regex_var.get().strip()
        }

        self.store.save()
        self.refresh_routing()

    def test_route_text(self):
        text = self.route_test_var.get()

        provider, reason = self.router.decide(
            text
        )

        candidates = self.router.candidates(
            provider
        )

        self.route_test_result_var.set(
            f"Selected: {provider}\n"
            f"Reason: {reason}\n"
            f"Candidates: {', '.join(candidates)}"
        )

    def test_current_route(self):
        text = self.prompt_box.get(
            "1.0",
            "end"
        ).strip() or "test this message"

        provider, reason = self.router.decide(
            text,
            None if self.provider_var.get() == "auto"
            else self.provider_var.get()
        )

        messagebox.showinfo(
            "Route",
            f"Provider: {provider}\nReason: {reason}"
        )

    # ----------------------------------------------------------
    # LOGS
    # ----------------------------------------------------------

    def show_selected_log(self, _event=None):
        selection = self.log_tree.selection()

        if not selection:
            return

        iid = selection[0]

        try:
            idx = int(iid)
        except Exception:
            return

        if idx >= len(
            getattr(self, "_all_visible_logs", [])
        ):
            return

        item = self._all_visible_logs[idx]

        self.log_detail.delete(
            "1.0",
            "end"
        )
        self.log_detail.insert(
            "1.0",
            pretty_json(item)
        )

    def export_logs(self):
        path = filedialog.asksaveasfilename(
            title="Export Logs",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")]
        )

        if not path:
            return

        save_json(
            Path(path),
            self.logger.read_all()
        )

    def clear_logs(self):
        if not messagebox.askyesno(
            "Clear Logs",
            "Delete all local logs?"
        ):
            return

        self.logger.clear()
        self.refresh_logs()

    # ----------------------------------------------------------
    # TOOL PLAYGROUND
    # ----------------------------------------------------------

    def open_tool_playground(self):
        win = tk.Toplevel(self)
        win.title("Tool Playground")
        win.geometry("850x600")

        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")

        tool_var = tk.StringVar(
            value="calculator"
        )

        ttk.Label(
            top,
            text="Tool"
        ).pack(side="left")

        ttk.Combobox(
            top,
            textvariable=tool_var,
            state="readonly",
            values=[
                "calculator",
                "current_time",
                "make_uuid",
                "sha256",
                "base64_encode",
                "base64_decode",
                "url_encode",
                "url_decode",
                "json_pretty",
                "http_get",
                "run_command"
            ],
            width=22
        ).pack(
            side="left",
            padx=8
        )

        input_box = tk.Text(
            win,
            height=8,
            wrap="word",
            font=("Consolas", 10)
        )
        input_box.pack(
            fill="x",
            padx=8,
            pady=8
        )

        output = tk.Text(
            win,
            wrap="word",
            font=("Consolas", 10)
        )
        output.pack(
            fill="both",
            expand=True,
            padx=8,
            pady=(0, 8)
        )

        def run():
            name = tool_var.get()
            arg = input_box.get(
                "1.0",
                "end"
            ).strip()

            try:
                fn = getattr(Tools, name)

                if name in {
                    "current_time",
                    "make_uuid"
                }:
                    value = fn()
                elif name == "calculator":
                    value = fn(arg)
                else:
                    value = fn(arg)

                output.delete(
                    "1.0",
                    "end"
                )
                output.insert(
                    "1.0",
                    str(value)
                )

            except Exception as exc:
                output.delete(
                    "1.0",
                    "end"
                )
                output.insert(
                    "1.0",
                    traceback.format_exc()
                )

        ttk.Button(
            top,
            text="RUN",
            command=run
        ).pack(side="left")

    # ----------------------------------------------------------
    # SETTINGS / SAVE
    # ----------------------------------------------------------

    def save_settings(self):
        try:
            history = int(
                self.settings_history_var.get()
            )
            timeout = int(
                self.settings_timeout_var.get()
            )
        except ValueError:
            messagebox.showerror(
                "Settings",
                "History and timeout must be integers."
            )
            return

        self.store.settings.update({
            "system_prompt": self.settings_prompt.get(
                "1.0",
                "end"
            ).strip(),
            "history_limit": history,
            "timeout": timeout,
            "verify_ssl": self.settings_verify_var.get(),
            "stream": self.settings_stream_var.get(),
            "autosave": self.settings_autosave_var.get(),
        })

        self.trim_history()
        self.persist_current_chat()
        self.store.save()

        messagebox.showinfo(
            "Settings",
            "Settings saved."
        )

    def save_everything(self):
        self.save_settings_silent()
        self.store.save()
        self.chat_status_var.set(
            "Everything saved."
        )

    def save_settings_silent(self):
        try:
            self.store.settings.update({
                "system_prompt": self.settings_prompt.get(
                    "1.0",
                    "end"
                ).strip(),
                "history_limit": int(
                    self.settings_history_var.get()
                ),
                "timeout": int(
                    self.settings_timeout_var.get()
                ),
                "verify_ssl": self.settings_verify_var.get(),
                "stream": self.settings_stream_var.get(),
                "autosave": self.settings_autosave_var.get(),
            })
        except Exception:
            pass

    def on_close(self):
        self.save_settings_silent()
        self.persist_current_chat()
        self.store.save()
        self.destroy()


# ============================================================
# GRAPH / AGENT SYSTEM
# ============================================================

class GraphEngine:
    """General-purpose branching/looping workflow executor."""

    MAX_STEPS = 1000

    def __init__(self, app):
        self.app = app

    def vars_template(self, value, vars_):
        if isinstance(value, str):
            exact = re.fullmatch(r"\{\{\s*([^}]+)\s*\}\}", value.strip())
            if exact:
                return nested_get(vars_, exact.group(1).strip(), "")
            def repl(m):
                v = nested_get(vars_, m.group(1).strip(), "")
                return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
            return re.sub(r"\{\{\s*([^}]+)\s*\}\}", repl, value)
        if isinstance(value, dict):
            return {k: self.vars_template(v, vars_) for k, v in value.items()}
        if isinstance(value, list):
            return [self.vars_template(v, vars_) for v in value]
        return value

    def cond(self, expression, vars_):
        ns = {
            "v": vars_,
            "vars": vars_,
            "json": json,
            "re": re,
            "len": len,
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "any": any,
            "all": all,
            "min": min,
            "max": max,
        }
        for k, val in vars_.items():
            if isinstance(k, str) and re.match(r"^[A-Za-z_]\w*$", k):
                ns.setdefault(k, val)
        return bool(eval(expression, {"__builtins__": {}}, ns))

    def node_map(self, graph):
        return {n["id"]: n for n in graph.get("nodes", [])}

    def edges_from(self, graph, node_id):
        return [e for e in graph.get("edges", []) if e.get("from") == node_id]

    def next_node(self, graph, node_id, vars_, branch="default"):
        edges = self.edges_from(graph, node_id)
        if not edges:
            return None

        # Exact branch first.
        for e in edges:
            if e.get("condition", "default") == branch:
                return e.get("to")

        # Expression edges.
        for e in edges:
            expr = e.get("expression", "").strip()
            if expr:
                try:
                    if self.cond(expr, vars_):
                        return e.get("to")
                except Exception:
                    pass

        for e in edges:
            if e.get("condition", "default") == "default":
                return e.get("to")
        return edges[0].get("to")

    def exec_python(self, script, vars_):
        ns = {
            "vars": vars_,
            "v": vars_,
            "json": json,
            "re": re,
            "os": os,
            "time": time,
            "uuid": uuid,
            "base64": base64,
            "hashlib": hashlib,
            "urllib": urllib,
            "subprocess": subprocess,
            "request": self.app.providers.http,
            "app": self.app,
            "result": None,
        }
        exec(script, ns, ns)
        return ns.get("result")

    def run_node(self, graph, node, vars_, emit):
        t = node.get("type", "noop")
        c = node.get("config", {})

        if t in {"start", "noop", "merge"}:
            return "default", None

        if t == "end":
            return "end", None

        if t == "set":
            val = self.vars_template(c.get("value", ""), vars_)
            nested_set(vars_, c.get("key", "value"), val)
            return "default", val

        if t == "template":
            val = self.vars_template(c.get("template", ""), vars_)
            vars_[c.get("output_key", "output")] = val
            return "default", val

        if t == "python":
            result = self.exec_python(c.get("script", ""), vars_)
            if c.get("output_key"):
                vars_[c["output_key"]] = result
            return "default", result

        if t == "condition":
            ok = self.cond(
                self.vars_template(c.get("expression", "False"), vars_),
                vars_
            )
            return ("true" if ok else "false"), ok

        if t == "switch":
            value = self.vars_template(c.get("value", "{{value}}"), vars_)
            case = str(value)
            cases = c.get("cases", {})
            return (case if case in cases else "default"), value

        if t == "api":
            provider_name = self.vars_template(c.get("provider", ""), vars_)
            if not provider_name:
                provider_name = self.app.store.routing.get("default", "")
            provider = self.app.providers.make(provider_name)
            prompt = self.vars_template(
                c.get("prompt", "{{last_user_message}}"),
                vars_
            )
            messages = []
            if c.get("use_history", True):
                messages.append({
                    "role": "system",
                    "content": self.app.store.settings.get("system_prompt", "")
                })
                for m in self.app.messages:
                    messages.append({"role": m.role, "content": m.content})
            else:
                messages = [{"role": "user", "content": prompt}]
            result = provider.send(
                messages,
                prompt,
                emit=emit if c.get("stream", True) else None
            )
            key = c.get("output_key", "answer")
            vars_[key] = result.text
            vars_[key + "_raw"] = result.raw
            vars_[key + "_provider"] = result.provider
            vars_[key + "_model"] = result.model
            return "default", result.text

        if t == "http":
            method = str(self.vars_template(c.get("method", "GET"), vars_)).upper()
            url = self.vars_template(c.get("url", ""), vars_)
            headers = self.vars_template(c.get("headers", {}), vars_)
            params = self.vars_template(c.get("params", {}), vars_)
            body = self.vars_template(c.get("body", {}), vars_)
            resp = self.app.providers.http.request(
                method, url, headers=headers, params=params,
                body=None if method in {"GET", "HEAD"} else body
            )
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            key = c.get("output_key", "http_result")
            vars_[key] = data
            vars_[key + "_status"] = resp.status
            return "default", data

        if t == "json":
            op = c.get("operation", "parse")
            inp = self.vars_template(c.get("input", ""), vars_)
            if op == "parse":
                result = json.loads(inp)
            elif op == "stringify":
                result = json.dumps(inp, ensure_ascii=False)
            elif op == "path":
                result = nested_get(inp, c.get("path", ""), "")
            else:
                raise ValueError(f"Unknown JSON operation: {op}")
            vars_[c.get("output_key", "json_result")] = result
            return "default", result

        if t == "delay":
            time.sleep(float(self.vars_template(str(c.get("seconds", 1)), vars_)))
            return "default", None

        if t == "shell":
            cmd = self.vars_template(c.get("command", ""), vars_)
            p = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=int(c.get("timeout", 120))
            )
            result = {
                "returncode": p.returncode,
                "stdout": p.stdout,
                "stderr": p.stderr
            }
            vars_[c.get("output_key", "shell_result")] = result
            return "default", result

        if t == "foreach":
            state = vars_.setdefault("__foreach__", {})
            key = node["id"]
            st = state.setdefault(
                key,
                {"i": 0, "items": self.vars_template(c.get("items", "[]"), vars_)}
            )
            items = st["items"]
            if not isinstance(items, list):
                raise ValueError("Foreach items must be a list.")
            if st["i"] >= len(items):
                state.pop(key, None)
                return "done", None
            idx = st["i"]
            item = items[idx]
            st["i"] += 1
            vars_[c.get("item_key", "item")] = item
            vars_[c.get("index_key", "index")] = idx
            return "body", item

        if t == "loop":
            state = vars_.setdefault("__loops__", {})
            key = node["id"]
            st = state.setdefault(key, {"i": 0})
            maximum = int(c.get("max_iterations", 50))
            if st["i"] >= maximum:
                state.pop(key, None)
                return "done", None
            ok = self.cond(
                self.vars_template(c.get("expression", "False"), vars_),
                vars_
            )
            if not ok:
                state.pop(key, None)
                return "done", None
            st["i"] += 1
            return "body", st["i"]

        if t == "ask":
            question = self.vars_template(
                c.get("question", "Continue?"),
                vars_
            )
            answer = messagebox.askyesno(
                c.get("title", "Agent"),
                question,
                parent=self.app
            )
            vars_[c.get("output_key", "answer")] = answer
            return ("yes" if answer else "no"), answer

        if t == "subgraph":
            name = self.vars_template(c.get("workflow", ""), vars_)
            result = self.run(
                name,
                vars_.copy(),
                emit
            )
            vars_[c.get("output_key", "subgraph_result")] = result
            return "default", result

        if t == "log":
            text = self.vars_template(c.get("text", "{{last_user_message}}"), vars_)
            self.app.logger.write(
                "agent_node_log",
                workflow=self.app.workflow_current_name,
                node=node["id"],
                text=text
            )
            emit(f"[log] {text}")
            return "default", text

        raise ValueError(f"Unknown node type: {t}")

    def run(self, workflow_name, variables=None, emit=None):
        workflows = self.app.store.data.setdefault("workflows", {})
        if workflow_name not in workflows:
            raise KeyError(f"Workflow '{workflow_name}' not found.")

        graph = workflows[workflow_name]
        nodes = self.node_map(graph)
        current = graph.get("start")

        if not current:
            starts = [n["id"] for n in graph.get("nodes", []) if n.get("type") == "start"]
            current = starts[0] if starts else None

        if not current:
            raise ValueError("No start node.")

        vars_ = dict(variables or {})
        steps = 0

        while current:
            steps += 1
            if steps > self.MAX_STEPS:
                raise RuntimeError("Graph exceeded maximum execution steps.")

            node = nodes.get(current)
            if not node:
                raise ValueError(f"Missing node: {current}")

            if emit:
                emit(f"→ {node['id']} [{node.get('type')}]")

            branch, result = self.run_node(
                graph,
                node,
                vars_,
                emit
            )

            if branch == "end":
                break

            current = self.next_node(
                graph,
                current,
                vars_,
                branch
            )

        return vars_


GRAPH_TEMPLATES = {
    "Basic Agent": {
        "nodes": [
            {"id":"start","type":"start","x":80,"y":220,"config":{}},
            {"id":"prompt","type":"template","x":300,"y":220,"config":{
                "output_key":"prompt","template":"{{last_user_message}}"
            }},
            {"id":"api","type":"api","x":540,"y":220,"config":{
                "provider":"{{workflow_provider}}",
                "prompt":"{{prompt}}","output_key":"answer","stream":True
            }},
            {"id":"end","type":"end","x":800,"y":220,"config":{}}
        ],
        "edges":[
            {"from":"start","to":"prompt"},
            {"from":"prompt","to":"api"},
            {"from":"api","to":"end"}
        ],
        "start":"start"
    },
    "Branching Agent": {
        "nodes":[
            {"id":"start","type":"start","x":60,"y":220,"config":{}},
            {"id":"if_code","type":"condition","x":280,"y":220,"config":{
                "expression":"'code' in last_user_message.lower() or 'python' in last_user_message.lower()"
            }},
            {"id":"coding","type":"api","x":520,"y":100,"config":{
                "provider":"{{coding_provider}}",
                "prompt":"Solve this as a coding expert: {{last_user_message}}",
                "output_key":"answer","stream":True
            }},
            {"id":"general","type":"api","x":520,"y":340,"config":{
                "provider":"{{general_provider}}",
                "prompt":"Answer normally: {{last_user_message}}",
                "output_key":"answer","stream":True
            }},
            {"id":"end","type":"end","x":800,"y":220,"config":{}}
        ],
        "edges":[
            {"from":"start","to":"if_code"},
            {"from":"if_code","to":"coding","condition":"true"},
            {"from":"if_code","to":"general","condition":"false"},
            {"from":"coding","to":"end"},
            {"from":"general","to":"end"}
        ],
        "start":"start"
    },
    "Research Fan-Out": {
        "nodes":[
            {"id":"start","type":"start","x":40,"y":260,"config":{}},
            {"id":"a","type":"api","x":280,"y":80,"config":{
                "provider":"{{provider_a}}","prompt":"Analyze independently: {{last_user_message}}",
                "output_key":"a","stream":False
            }},
            {"id":"b","type":"api","x":280,"y":300,"config":{
                "provider":"{{provider_b}}","prompt":"Analyze independently from another perspective: {{last_user_message}}",
                "output_key":"b","stream":False
            }},
            {"id":"combine","type":"template","x":550,"y":190,"config":{
                "output_key":"synthesis",
                "template":"A:\\n{{a}}\\n\\nB:\\n{{b}}\\n\\nSynthesize these."
            }},
            {"id":"final","type":"api","x":800,"y":190,"config":{
                "provider":"{{synthesis_provider}}","prompt":"{{synthesis}}",
                "output_key":"answer","stream":True
            }},
            {"id":"end","type":"end","x":1050,"y":190,"config":{}}
        ],
        "edges":[
            {"from":"start","to":"a"},
            {"from":"start","to":"b"},
            {"from":"a","to":"combine"},
            {"from":"b","to":"combine"},
            {"from":"combine","to":"final"},
            {"from":"final","to":"end"}
        ],
        "start":"start"
    },
    "Foreach Agent": {
        "nodes":[
            {"id":"start","type":"start","x":40,"y":220,"config":{}},
            {"id":"each","type":"foreach","x":250,"y":220,"config":{
                "items":"{{items}}","item_key":"item","index_key":"index"
            }},
            {"id":"api","type":"api","x":500,"y":100,"config":{
                "provider":"{{provider}}","prompt":"Process item: {{item}}",
                "output_key":"current","stream":False
            }},
            {"id":"append","type":"python","x":740,"y":100,"config":{
                "output_key":"results",
                "script":
                    "vars.setdefault('results', [])\n"
                    "vars['results'].append(vars.get('current'))\n"
                    "result = vars['results']"
            }},
            {"id":"end","type":"end","x":980,"y":260,"config":{}}
        ],
        "edges":[
            {"from":"start","to":"each"},
            {"from":"each","to":"api","condition":"body"},
            {"from":"each","to":"end","condition":"done"},
            {"from":"api","to":"append"},
            {"from":"append","to":"each"}
        ],
        "start":"start"
    },
    "Retry Loop": {
        "nodes":[
            {"id":"start","type":"start","x":40,"y":220,"config":{}},
            {"id":"set","type":"set","x":230,"y":220,"config":{
                "key":"attempt","value":0
            }},
            {"id":"inc","type":"python","x":420,"y":220,"config":{
                "script":
                    "vars['attempt'] = vars.get('attempt', 0) + 1\n"
                    "result = vars['attempt']",
                "output_key":"attempt"
            }},
            {"id":"api","type":"api","x":620,"y":220,"config":{
                "provider":"{{provider}}","prompt":"{{last_user_message}}",
                "output_key":"answer","stream":False
            }},
            {"id":"check","type":"condition","x":820,"y":220,"config":{
                "expression":"bool(answer) and len(str(answer)) > 10"
            }},
            {"id":"end","type":"end","x":1050,"y":110,"config":{}},
            {"id":"loop","type":"loop","x":1050,"y":330,"config":{
                "expression":"attempt < 3","max_iterations":3
            }}
        ],
        "edges":[
            {"from":"start","to":"set"},
            {"from":"set","to":"inc"},
            {"from":"inc","to":"api"},
            {"from":"api","to":"check"},
            {"from":"check","to":"end","condition":"true"},
            {"from":"check","to":"loop","condition":"false"},
            {"from":"loop","to":"inc","condition":"body"},
            {"from":"loop","to":"end","condition":"done"}
        ],
        "start":"start"
    },
    "API + Transform": {
        "nodes":[
            {"id":"start","type":"start","x":60,"y":220,"config":{}},
            {"id":"http","type":"http","x":280,"y":220,"config":{
                "method":"GET","url":"{{url}}","output_key":"data"
            }},
            {"id":"python","type":"python","x":520,"y":220,"config":{
                "output_key":"processed",
                "script":
                    "data = vars.get('data')\n"
                    "result = data"
            }},
            {"id":"json","type":"json","x":760,"y":220,"config":{
                "operation":"stringify","input":"{{processed}}","output_key":"text"
            }},
            {"id":"end","type":"end","x":1000,"y":220,"config":{}}
        ],
        "edges":[
            {"from":"start","to":"http"},
            {"from":"http","to":"python"},
            {"from":"python","to":"json"},
            {"from":"json","to":"end"}
        ],
        "start":"start"
    }
}


# ============================================================
# VISUAL GRAPH DESIGNER
# ============================================================

class GraphDesigner(tk.Toplevel):
    W = 180
    H = 84

    def __init__(self, app, name=None):
        super().__init__(app)
        self.app = app
        self.title("Agent Graph Designer")
        self.geometry("1500x900")
        self.name = name or "New Agent"
        self.drag = None
        self.selected = None
        self.connect_from = None

        if name and name in app.store.data.get("workflows", {}):
            self.graph = clone(app.store.data["workflows"][name])
        else:
            self.graph = clone(GRAPH_TEMPLATES["Basic Agent"])

        self.build()
        self.redraw()

    def build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")

        ttk.Label(top, text="Graph Name").pack(side="left")
        self.name_var = tk.StringVar(value=self.name)
        ttk.Entry(top, textvariable=self.name_var, width=28).pack(side="left", padx=6)

        for text, cmd in [
            ("Save", self.save),
            ("Run", self.run),
            ("Templates", self.templates),
            ("Validate", self.validate)
        ]:
            ttk.Button(top, text=text, command=cmd).pack(side="left", padx=2)

        ttk.Label(
            top,
            text="Drag nodes | select node | right-click edit | Connect mode then click target"
        ).pack(side="right")

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=6, pady=6)

        palette = ttk.Frame(panes, padding=5)
        center = ttk.Frame(panes, padding=5)
        inspector = ttk.Frame(panes, padding=5)

        panes.add(palette, weight=1)
        panes.add(center, weight=5)
        panes.add(inspector, weight=2)

        ttk.Label(palette, text="Nodes", style="Heading.TLabel").pack(anchor="w")
        for ntype in [
            "start","end","template","api","http","python","condition","switch",
            "loop","foreach","set","json","log","delay","shell","merge","ask",
            "subgraph","noop"
        ]:
            ttk.Button(
                palette,
                text=ntype.upper(),
                command=lambda t=ntype: self.add_node(t)
            ).pack(fill="x", pady=2)

        ttk.Separator(palette, orient="horizontal").pack(fill="x", pady=8)
        ttk.Button(palette, text="CONNECT SELECTED", command=self.connect_mode).pack(fill="x")
        ttk.Button(palette, text="DELETE SELECTED", command=self.delete_selected).pack(fill="x", pady=3)
        ttk.Button(palette, text="RAW GRAPH JSON", command=self.raw_json).pack(fill="x")

        self.canvas = tk.Canvas(center, bg="#15171c", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.click)
        self.canvas.bind("<B1-Motion>", self.drag_node)
        self.canvas.bind("<ButtonRelease-1>", lambda _e: setattr(self, "drag", None))
        self.canvas.bind("<Button-3>", self.right_click)

        ttk.Label(inspector, text="Selected Node", style="Heading.TLabel").pack(anchor="w")
        self.inspector = tk.Text(inspector, font=("Consolas", 9), wrap="word")
        self.inspector.pack(fill="both", expand=True, pady=5)
        ttk.Button(inspector, text="APPLY JSON", command=self.apply_inspector).pack(fill="x")
        ttk.Label(
            inspector,
            text=(
                "Edges can carry branch labels: true / false / body / done / yes / no "
                "or any custom switch case. Edit edge labels in RAW GRAPH JSON."
            ),
            wraplength=300,
            justify="left"
        ).pack(fill="x", pady=8)

    def add_node(self, ntype):
        defaults = {
            "start": {},
            "end": {},
            "template": {"output_key":"output","template":"{{last_user_message}}"},
            "api": {"provider":"{{workflow_provider}}","prompt":"{{last_user_message}}","output_key":"answer","stream":True,"use_history":True},
            "http": {"method":"GET","url":"","headers":{},"params":{},"body":{},"output_key":"http_result"},
            "python": {"output_key":"result","script":"result = vars.get('value')"},
            "condition": {"expression":"False"},
            "switch": {"value":"{{value}}","cases":{"yes":"yes","no":"no"}},
            "loop": {"expression":"counter < 3","max_iterations":3},
            "foreach": {"items":"{{items}}","item_key":"item","index_key":"index"},
            "set": {"key":"value","value":""},
            "json": {"operation":"parse","input":"{{text}}","path":"","output_key":"json_result"},
            "log": {"text":"{{last_user_message}}"},
            "delay": {"seconds":1},
            "shell": {"command":"echo {{last_user_message}}","timeout":120,"output_key":"shell_result"},
            "merge": {},
            "ask": {"title":"Agent","question":"Continue?","output_key":"answer"},
            "subgraph": {"workflow":"","output_key":"subgraph_result"},
            "noop": {}
        }

        node_id = f"node_{uuid.uuid4().hex[:7]}"
        node = {
            "id": node_id,
            "type": ntype,
            "x": 100 + 40 * len(self.graph.get("nodes", [])),
            "y": 120 + 25 * len(self.graph.get("nodes", [])),
            "config": clone(defaults.get(ntype, {}))
        }
        self.graph.setdefault("nodes", []).append(node)
        self.selected = node_id
        self.show_inspector()
        self.redraw()

    def node_at(self, x, y):
        for node in reversed(self.graph.get("nodes", [])):
            nx, ny = node.get("x", 100), node.get("y", 100)
            if nx <= x <= nx + self.W and ny <= y <= ny + self.H:
                return node
        return None

    def redraw(self):
        self.canvas.delete("all")
        # grid
        width = max(self.canvas.winfo_width(), 2500)
        height = max(self.canvas.winfo_height(), 1800)
        for x in range(0, width, 20):
            self.canvas.create_line(x, 0, x, height, fill="#20232a")
        for y in range(0, height, 20):
            self.canvas.create_line(0, y, width, y, fill="#20232a")

        nodes = {n["id"]: n for n in self.graph.get("nodes", [])}

        for edge in self.graph.get("edges", []):
            a, b = nodes.get(edge.get("from")), nodes.get(edge.get("to"))
            if not a or not b:
                continue
            x1, y1 = a["x"] + self.W, a["y"] + self.H/2
            x2, y2 = b["x"], b["y"] + self.H/2
            mx = (x1 + x2) / 2
            self.canvas.create_line(
                x1,y1,mx,y1,mx,y2,x2,y2,
                fill="#8f98ac", width=2, arrow=tk.LAST, smooth=True
            )
            lab = edge.get("condition", "")
            if edge.get("expression"):
                lab = edge["expression"]
            if lab != "default":
                self.canvas.create_text(
                    mx, (y1+y2)/2, text=str(lab),
                    fill="#ddd", font=("TkDefaultFont",8)
                )

        colors = {
            "start":"#79ce90","end":"#e88484","api":"#b69be6","http":"#b69be6",
            "python":"#e2b266","condition":"#e4db78","switch":"#e4db78",
            "loop":"#e4db78","foreach":"#e4db78","template":"#77b5e5",
            "set":"#99cf8b","json":"#8fc7bc","shell":"#db936f","ask":"#d39cbc",
            "subgraph":"#ce94c7","merge":"#aaa9d6","log":"#aaa","delay":"#aaa","noop":"#aaa"
        }

        for node in self.graph.get("nodes", []):
            x,y = node.get("x",100), node.get("y",100)
            t=node.get("type","noop")
            outline = "#fff" if node["id"] == self.selected else "#111"
            self.canvas.create_rectangle(
                x,y,x+self.W,y+self.H,
                fill=colors.get(t,"#aaa"),
                outline=outline,
                width=3 if node["id"] == self.selected else 1
            )
            self.canvas.create_text(
                x+self.W/2, y+23,
                text=t.upper()+"\n"+node["id"],
                fill="#151515",
                font=("TkDefaultFont",9,"bold")
            )

            preview = ""
            c = node.get("config", {})
            if t == "api":
                preview = str(c.get("provider",""))
            elif t in {"condition","loop"}:
                preview = str(c.get("expression",""))
            elif t == "http":
                preview = str(c.get("url",""))
            elif t == "template":
                preview = str(c.get("template",""))

            if preview:
                self.canvas.create_text(
                    x+7,y+60,text=preview[:25],
                    anchor="w",fill="#222",font=("Consolas",7)
                )

    def click(self, event):
        node = self.node_at(event.x, event.y)
        if node:
            if self.connect_from and node["id"] != self.connect_from:
                self.graph.setdefault("edges", []).append({
                    "from": self.connect_from,
                    "to": node["id"],
                    "condition": "default"
                })
                self.connect_from = None
            else:
                self.selected = node["id"]
                self.drag = (node, event.x-node["x"], event.y-node["y"])
                self.show_inspector()
            self.redraw()

    def drag_node(self, event):
        if not self.drag:
            return
        node,dx,dy = self.drag
        node["x"] = max(0, event.x-dx)
        node["y"] = max(0, event.y-dy)
        self.redraw()

    def right_click(self, event):
        node = self.node_at(event.x, event.y)
        if node:
            self.selected = node["id"]
            self.show_inspector()
            self.redraw()

    def connect_mode(self):
        if not self.selected:
            return
        self.connect_from = self.selected
        self.title("Agent Graph Designer - click a target node")

    def delete_selected(self):
        if not self.selected:
            return
        self.graph["nodes"] = [n for n in self.graph.get("nodes", []) if n["id"] != self.selected]
        self.graph["edges"] = [
            e for e in self.graph.get("edges", [])
            if e.get("from") != self.selected and e.get("to") != self.selected
        ]
        if self.graph.get("start") == self.selected:
            starts = [n["id"] for n in self.graph["nodes"] if n.get("type")=="start"]
            self.graph["start"] = starts[0] if starts else None
        self.selected = None
        self.redraw()

    def show_inspector(self):
        self.inspector.delete("1.0","end")
        for n in self.graph.get("nodes", []):
            if n["id"] == self.selected:
                self.inspector.insert("1.0", pretty(n))
                break

    def apply_inspector(self):
        if not self.selected:
            return
        try:
            edited = json.loads(self.inspector.get("1.0","end"))
            if edited.get("id") != self.selected:
                raise ValueError("Node ID cannot be changed here.")
            for i,n in enumerate(self.graph["nodes"]):
                if n["id"] == self.selected:
                    self.graph["nodes"][i] = edited
                    break
            self.redraw()
        except Exception as e:
            messagebox.showerror("Inspector", str(e), parent=self)

    def raw_json(self):
        win = tk.Toplevel(self)
        win.title("Raw Graph JSON")
        win.geometry("900x700")
        txt = tk.Text(win, font=("Consolas",9))
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", pretty(self.graph))

    def templates(self):
        win = tk.Toplevel(self)
        win.title("Graph Templates")
        win.geometry("750x520")
        names = list(GRAPH_TEMPLATES)

        lb=tk.Listbox(win)
        lb.pack(side="left",fill="both",expand=True,padx=8,pady=8)
        for n in names:
            lb.insert("end",n)

        right=ttk.Frame(win,padding=8)
        right.pack(side="left",fill="both",expand=True)

        desc=tk.StringVar()
        ttk.Label(right,textvariable=desc,wraplength=350,justify="left").pack(fill="x",pady=10)

        descriptions = {
            "Basic Agent":"Simple prompt -> API -> end.",
            "Branching Agent":"Conditionally routes to different agents.",
            "Research Fan-Out":"Multiple branches feed a synthesis stage.",
            "Foreach Agent":"Iterates a list and processes every item.",
            "Retry Loop":"Loops until an answer passes a condition or retries run out.",
            "API + Transform":"HTTP -> Python -> JSON."
        }

        def update(_e=None):
            s=lb.curselection()
            if s: desc.set(descriptions.get(names[s[0]],""))
        lb.bind("<<ListboxSelect>>",update)

        def use():
            s=lb.curselection()
            if not s: return
            name=names[s[0]]
            self.graph=clone(GRAPH_TEMPLATES[name])
            self.name_var.set(name)
            self.selected=None
            self.redraw()
            win.destroy()

        ttk.Button(right,text="USE TEMPLATE",command=use).pack(fill="x")

    def validate(self):
        ids={n["id"] for n in self.graph.get("nodes",[])}
        errors=[]
        if self.graph.get("start") not in ids:
            errors.append("Start node is missing.")
        for e in self.graph.get("edges",[]):
            if e.get("from") not in ids or e.get("to") not in ids:
                errors.append(f"Broken edge: {e}")
        if errors:
            messagebox.showerror("Validation","\n".join(errors),parent=self)
        else:
            messagebox.showinfo(
                "Validation",
                f"Graph OK: {len(ids)} nodes, {len(self.graph.get('edges',[]))} edges.",
                parent=self
            )

    def save(self):
        name=self.name_var.get().strip()
        if not name:
            messagebox.showerror("Save","Graph name required.",parent=self)
            return
        self.app.store.data.setdefault("workflows",{})[name]=clone(self.graph)
        self.app.store.save()
        self.name=name
        self.app.refresh_workflows()
        self.title(f"Agent Graph Designer - {name}")

    def run(self):
        name=self.name_var.get().strip() or "Temporary"
        self.app.store.data.setdefault("workflows",{})[name]=clone(self.graph)
        vars_={
            "last_user_message":self.app.input_box.get("1.0","end").strip()
            if hasattr(self.app,"prompt_box") else "",
            "workflow_provider":(
                self.app.provider_var.get()
                if self.app.provider_var.get()!="auto"
                else self.app.store.routing.get("default","")
            )
        }

        win=tk.Toplevel(self)
        win.title("Workflow Run")
        win.geometry("800x600")
        out=tk.Text(win,font=("Consolas",9))
        out.pack(fill="both",expand=True)

        def emit(line):
            self.app.after(0,lambda:(out.insert("end",str(line)+"\n"),out.see("end")))

        def worker():
            try:
                result=self.app.graph_engine.run(name,vars_,emit)
                self.app.after(0,lambda:out.insert("end","\nFINAL VARIABLES:\n"+pretty(result)))
            except Exception:
                err=traceback.format_exc()
                self.app.after(0,lambda:out.insert("end","\nERROR:\n"+err))

        threading.Thread(target=worker,daemon=True).start()



    # ----------------------------------------------------------
    # LOGS TAB
    # ----------------------------------------------------------

    def _build_logs_tab(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="Logs")

        controls = ttk.Frame(tab)
        controls.pack(fill="x")

        ttk.Label(
            controls,
            text="Search"
        ).pack(side="left")

        self.log_search_var = tk.StringVar()
        ttk.Entry(
            controls,
            textvariable=self.log_search_var
        ).pack(
            side="left",
            fill="x",
            expand=True,
            padx=6
        )

        self.log_event_var = tk.StringVar(value="ALL")
        ttk.Combobox(
            controls,
            textvariable=self.log_event_var,
            state="readonly",
            width=18,
            values=["ALL"]
        ).pack(side="left", padx=6)

        ttk.Button(
            controls,
            text="Refresh",
            command=self.refresh_logs
        ).pack(side="left")

        ttk.Button(
            controls,
            text="Export",
            command=self.export_logs
        ).pack(side="left", padx=4)

        ttk.Button(
            controls,
            text="Clear",
            command=self.clear_logs
        ).pack(side="left")

        paned = ttk.PanedWindow(
            tab,
            orient="vertical"
        )
        paned.pack(
            fill="both",
            expand=True,
            pady=(8, 0)
        )

        top = ttk.Frame(paned)
        bottom = ttk.Frame(paned)

        paned.add(top, weight=2)
        paned.add(bottom, weight=1)

        columns = (
            "timestamp",
            "event",
            "provider",
            "request_id",
        )

        self.log_tree = ttk.Treeview(
            top,
            columns=columns,
            show="headings"
        )

        for col, width in [
            ("timestamp", 160),
            ("event", 160),
            ("provider", 160),
            ("request_id", 120),
        ]:
            self.log_tree.heading(col, text=col.upper())
            self.log_tree.column(col, width=width)

        self.log_tree.pack(
            fill="both",
            expand=True
        )

        self.log_tree.bind(
            "<<TreeviewSelect>>",
            self.show_selected_log
        )

        self.log_detail = tk.Text(
            bottom,
            wrap="word",
            font=("Consolas", 9)
        )
        self.log_detail.pack(
            fill="both",
            expand=True
        )

    # ----------------------------------------------------------
    # TOOLS TAB
    # ----------------------------------------------------------

    def _build_tools_tab(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="Tools")

        ttk.Label(
            tab,
            text="Built-in local utility tools",
            style="Heading.TLabel"
        ).pack(anchor="w")

        self.tool_text = tk.Text(
            tab,
            wrap="word",
            font=("Consolas", 10)
        )
        self.tool_text.pack(
            fill="both",
            expand=True,
            pady=8
        )

        ttk.Button(
            tab,
            text="Open Tool Playground",
            command=self.open_tool_playground
        ).pack(anchor="w")

        ttk.Label(
            tab,
            text=(
                "The tool functions live in the Tools class in this same file. "
                "Add your own API calls, filesystem helpers, database calls, "
                "scripts, web requests, etc. there."
            ),
            wraplength=900,
            justify="left"
        ).pack(
            anchor="w",
            pady=8
        )

    # ----------------------------------------------------------
    # SETTINGS TAB
    # ----------------------------------------------------------

    def _build_settings_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Settings")

        self.settings_prompt = tk.Text(
            tab,
            height=8,
            wrap="word"
        )
        self.settings_prompt.pack(
            fill="x",
            pady=(0, 10)
        )

        grid = ttk.Frame(tab)
        grid.pack(fill="x")

        self.settings_history_var = tk.StringVar()
        self.settings_timeout_var = tk.StringVar()
        self.settings_verify_var = tk.BooleanVar()
        self.settings_stream_var = tk.BooleanVar()
        self.settings_autosave_var = tk.BooleanVar()

        rows = [
            (
                "History messages",
                self.settings_history_var
            ),
            (
                "HTTP timeout (seconds)",
                self.settings_timeout_var
            ),
        ]

        for i, (label, var) in enumerate(rows):
            ttk.Label(
                grid,
                text=label
            ).grid(
                row=i, column=0, sticky="w", pady=4
            )
            ttk.Entry(
                grid,
                textvariable=var,
                width=20
            ).grid(
                row=i, column=1, sticky="w", padx=8
            )

        ttk.Checkbutton(
            grid,
            text="Verify SSL",
            variable=self.settings_verify_var
        ).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=4
        )

        ttk.Checkbutton(
            grid,
            text="Stream OpenAI-compatible responses",
            variable=self.settings_stream_var
        ).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=4
        )

        ttk.Checkbutton(
            grid,
            text="Autosave",
            variable=self.settings_autosave_var
        ).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=4
        )

        ttk.Button(
            tab,
            text="Save Settings",
            command=self.save_settings
        ).pack(
            anchor="w",
            pady=10
        )

        ttk.Label(
            tab,
            text=(
                f"Data file: {DATA_FILE}\n"
                f"Log file: {LOG_FILE}"
            ),
            wraplength=1000,
            justify="left"
        ).pack(
            anchor="w",
            pady=10
        )

    # ----------------------------------------------------------
    # REFRESH
    # ----------------------------------------------------------

    def refresh_all(self):
        self.refresh_provider_lists()
        self.refresh_provider_editor_list()
        self.refresh_routing()
        self.refresh_workflows()
        self.refresh_logs()
        self.refresh_tools()
        self.refresh_settings()
        self.render_chat()
        self.update_api_editor_help()

    def refresh_provider_lists(self):
        names = list(self.store.providers.keys())

        self.provider_combo["values"] = [
            "auto"
        ] + names

        if self.provider_var.get() not in ["auto"] + names:
            self.provider_var.set("auto")

        self.route_default_combo["values"] = names

    def refresh_provider_editor_list(self):
        current = self.api_name_var.get()

        self.provider_list.delete(
            0,
            "end"
        )

        for name, cfg in self.store.providers.items():
            marker = "●" if cfg.get("enabled", True) else "○"
            self.provider_list.insert(
                "end",
                f"{marker} {name}"
            )

        names = list(self.store.providers.keys())

        if current in names:
            idx = names.index(current)
            self.provider_list.selection_clear(0, "end")
            self.provider_list.selection_set(idx)
            self.provider_list.see(idx)

    def refresh_routing(self):
        names = list(self.store.providers.keys())

        self.route_default_combo["values"] = names

        default = self.store.routing.get("default", "")
        self.route_default_var.set(default)

        self.route_fallback_var.set(
            ", ".join(
                self.store.routing.get("fallbacks", [])
            )
        )

        self.rule_list.delete(
            0,
            "end"
        )

        for rule in self.store.routing.get("rules", []):
            self.rule_list.insert(
                "end",
                rule.get("name", "unnamed")
            )

    def refresh_logs(self):
        records = self.logger.read_all()

        event_names = ["ALL"] + sorted(
            {
                x.get("event", "unknown")
                for x in records
            }
        )

        combo = None

        # Find the log event combobox without retaining another reference.
        for child in self.notebook.winfo_children():
            # no-op; the explicit values update is done using widget search below
            pass

        # The widget was not assigned to an instance in the initial version;
        # locate it among the controls for simplicity.
        log_search = self.log_search_var.get().lower()
        selected_event = self.log_event_var.get()

        self.log_tree.delete(
            *self.log_tree.get_children()
        )

        for idx, item in enumerate(reversed(records)):
            if selected_event != "ALL" and item.get("event") != selected_event:
                continue

            blob = pretty_json(item).lower()

            if log_search and log_search not in blob:
                continue

            iid = str(idx)

            self.log_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    item.get("timestamp", ""),
                    item.get("event", ""),
                    item.get("provider", ""),
                    item.get("request_id", ""),
                )
            )

        self._all_visible_logs = list(reversed(records))

        self.tool_log_events = event_names

    def refresh_tools(self):
        text = (
            "calculator(expression)\n"
            "  Basic arithmetic.\n\n"
            "current_time()\n"
            "  Local machine time.\n\n"
            "make_uuid()\n"
            "  Generate UUID4.\n\n"
            "sha256(text)\n"
            "  SHA-256 hash.\n\n"
            "base64_encode(text)\n"
            "base64_decode(text)\n\n"
            "url_encode(text)\n"
            "url_decode(text)\n\n"
            "json_pretty(text)\n"
            "  Pretty-print JSON.\n\n"
            "http_get(url)\n"
            "  Simple HTTP GET.\n\n"
            "run_command(command)\n"
            "  Local shell command execution; intentionally exposed only "
            "as a local POC utility.\n"
        )

        self.tool_text.delete(
            "1.0",
            "end"
        )
        self.tool_text.insert(
            "1.0",
            text
        )

    def refresh_settings(self):
        settings = self.store.settings

        self.settings_prompt.delete(
            "1.0",
            "end"
        )
        self.settings_prompt.insert(
            "1.0",
            settings.get("system_prompt", "")
        )

        self.settings_history_var.set(
            str(settings.get("history_limit", 40))
        )
        self.settings_timeout_var.set(
            str(settings.get("timeout", 120))
        )

        self.settings_verify_var.set(
            bool(settings.get("verify_ssl", True))
        )

        self.settings_stream_var.set(
            bool(settings.get("stream", True))
        )

        self.settings_autosave_var.set(
            bool(settings.get("autosave", True))
        )

    # ----------------------------------------------------------
    # CHAT
    # ----------------------------------------------------------

    def render_chat(self):
        self.chat_view.configure(
            state="normal"
        )
        self.chat_view.delete(
            "1.0",
            "end"
        )

        for m in self.messages:
            tag = (
                "user"
                if m.role == "user"
                else "assistant"
                if m.role == "assistant"
                else "system"
            )

            label = (
                "YOU"
                if m.role == "user"
                else "AI"
                if m.role == "assistant"
                else "SYSTEM"
            )

            self.chat_view.insert(
                "end",
                f"{label}  {m.timestamp}\n",
                tag
            )
            self.chat_view.insert(
                "end",
                m.content + "\n\n"
            )

        self.chat_view.configure(
            state="disabled"
        )
        self.chat_view.see("end")

    def append_chat(self, role: str, content: str):
        tag = (
            "user"
            if role == "user"
            else "assistant"
            if role == "assistant"
            else "error"
        )

        label = (
            "YOU"
            if role == "user"
            else "AI"
            if role == "assistant"
            else "ERROR"
        )

        self.chat_view.configure(
            state="normal"
        )
        self.chat_view.insert(
            "end",
            f"{label}  {now()}\n",
            tag
        )
        self.chat_view.insert(
            "end",
            content + "\n\n"
        )
        self.chat_view.configure(
            state="disabled"
        )
        self.chat_view.see("end")

    def send_message(self):
        if self.busy:
            return

        text = self.prompt_box.get(
            "1.0",
            "end"
        ).strip()

        if not text:
            return

        # Local slash commands.
        if self.handle_command(text):
            self.prompt_box.delete("1.0", "end")
            return

        self.prompt_box.delete(
            "1.0",
            "end"
        )

        self.messages.append(
            Message(
                role="user",
                content=text
            )
        )

        self.trim_history()
        self.append_chat(
            "user",
            text
        )

        self.busy = True
        self.send_button.configure(
            state="disabled"
        )
        self.chat_status_var.set(
            "Routing..."
        )

        forced = self.provider_var.get()
        if forced == "auto":
            forced = None

        threading.Thread(
            target=self._send_worker,
            args=(text, forced),
            daemon=True
        ).start()

    def _send_worker(
        self,
        text: str,
        forced: Optional[str]
    ):
        try:
            provider, reason = self.router.decide(
                text,
                forced
            )

            candidates = self.router.candidates(
                provider
            )

            self.logger.write(
                "route",
                message=text,
                selected=provider,
                reason=reason,
                candidates=candidates
            )

            self.after(
                0,
                lambda: self.chat_status_var.set(
                    f"Route: {provider} ({reason})"
                )
            )

            # Context = system + conversation.
            outgoing = [
                Message(
                    role="system",
                    content=self.store.settings.get(
                        "system_prompt",
                        ""
                    )
                )
            ]

            outgoing.extend(self.messages)

            use_stream = bool(
                self.store.settings.get(
                    "stream",
                    True
                )
            )

            last_exc = None

            for candidate in candidates:
                try:
                    provider_obj = self.providers.get(
                        candidate
                    )

                    response = provider_obj.send(
                        outgoing,
                        stream=use_stream
                        and isinstance(
                            provider_obj,
                            OpenAICompatibleProvider
                        ),
                        on_token=lambda token: self.after(
                            0,
                            self._insert_stream_token,
                            token
                        )
                    )

                    self.after(
                        0,
                        self._finish_response,
                        response
                    )
                    return

                except Exception as exc:
                    last_exc = exc
                    self.logger.write(
                        "provider_failed",
                        provider=candidate,
                        error=repr(exc)
                    )

            raise RuntimeError(
                f"All routes failed. Last error: {last_exc}"
            )

        except Exception as exc:
            self.after(
                0,
                self._handle_send_error,
                str(exc)
            )

    def _insert_stream_token(self, token: str):
        if self.chat_response_start is None:
            self.chat_view.configure(
                state="normal"
            )
            self.chat_view.insert(
                "end",
                f"AI  {now()}\n",
                "assistant"
            )
            self.chat_response_start = (
                self.chat_view.index("end-1c")
            )
            self.chat_view.configure(
                state="disabled"
            )

        self.chat_view.configure(
            state="normal"
        )
        self.chat_view.insert(
            "end",
            token
        )
        self.chat_view.configure(
            state="disabled"
        )
        self.chat_view.see("end")

    def _finish_response(
        self,
        response: ProviderResponse
    ):
        self.chat_response_start = None

        # When streaming is active, text was already rendered.
        streaming = bool(
            self.store.settings.get("stream", True)
        ) and response.provider in self.store.providers \
            and self.store.providers[response.provider].get(
                "type"
            ) == "openai_compatible"

        if not streaming:
            self.append_chat(
                "assistant",
                response.text
            )

        self.messages.append(
            Message(
                role="assistant",
                content=response.text,
                meta={
                    "provider": response.provider,
                    "model": response.model,
                    "usage": response.usage
                }
            )
        )

        self.trim_history()
        self.persist_current_chat()

        self.busy = False
        self.send_button.configure(
            state="normal"
        )

        self.chat_status_var.set(
            f"Done: {response.provider}"
            + (
                f" / {response.model}"
                if response.model
                else ""
            )
        )

    def _handle_send_error(self, text: str):
        self.busy = False
        self.send_button.configure(
            state="normal"
        )
        self.append_chat(
            "error",
            text
        )
        self.chat_status_var.set(
            "Error"
        )

    def trim_history(self):
        limit = int(
            self.store.settings.get(
                "history_limit",
                40
            )
        )

        if len(self.messages) > limit:
            self.messages = self.messages[-limit:]

    def persist_current_chat(self):
        self.store.save_messages(
            self.current_chat,
            self.messages
        )

        if self.store.settings.get(
            "autosave",
            True
        ):
            self.store.save()

    def clear_current_chat(self):
        self.messages = []
        self.persist_current_chat()
        self.render_chat()

    def new_chat(self):
        name = simpledialog.askstring(
            "New Chat",
            "Chat name:",
            parent=self
        )

        if not name:
            return

        if name in self.store.chats:
            messagebox.showerror(
                "Exists",
                "A chat with that name already exists."
            )
            return

        self.store.chats[name] = []
        self.store.save()
        self.current_chat = name
        self.chat_var.set(name)
        self.messages = []
        self.refresh_chat_combo()

    def rename_chat(self):
        old = self.current_chat

        new = simpledialog.askstring(
            "Rename Chat",
            "New name:",
            initialvalue=old,
            parent=self
        )

        if not new or new == old:
            return

        if new in self.store.chats:
            messagebox.showerror(
                "Exists",
                "A chat with that name already exists."
            )
            return

        self.store.chats[new] = self.store.chats.pop(old)
        self.current_chat = new
        self.chat_var.set(new)
        self.store.save()
        self.refresh_chat_combo()

    def delete_chat(self):
        if len(self.store.chats) <= 1:
            messagebox.showinfo(
                "Delete Chat",
                "Keep at least one chat."
            )
            return

        if not messagebox.askyesno(
            "Delete Chat",
            f"Delete '{self.current_chat}'?"
        ):
            return

        del self.store.chats[
            self.current_chat
        ]

        self.current_chat = next(
            iter(self.store.chats)
        )

        self.chat_var.set(
            self.current_chat
        )

        self.messages = self.store.load_messages(
            self.current_chat
        )

        self.store.save()
        self.refresh_chat_combo()

    def refresh_chat_combo(self):
        self.chat_combo["values"] = list(
            self.store.chats.keys()
        )
        self.chat_combo.set(
            self.current_chat
        )
        self.render_chat()

    def change_chat(self, _event=None):
        self.current_chat = (
            self.chat_var.get()
        )
        self.messages = self.store.load_messages(
            self.current_chat
        )
        self.render_chat()

    def export_chat(self):
        path = filedialog.asksaveasfilename(
            title="Export Chat",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")]
        )
        if not path:
            return

        save_json(
            Path(path),
            {
                "chat": self.current_chat,
                "messages": [
                    asdict(m)
                    for m in self.messages
                ]
            }
        )

    # ----------------------------------------------------------
    # COMMANDS
    # ----------------------------------------------------------

    def handle_command(self, text: str) -> bool:
        if not text.startswith("/"):
            return False

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/help":
            self.append_chat(
                "system",
                (
                    "/help\n"
                    "/clear\n"
                    "/provider NAME\n"
                    "/route TEXT\n"
                    "/apis\n"
                    "/logs"
                )
            )
            return True

        if cmd == "/clear":
            self.clear_current_chat()
            return True

        if cmd == "/provider":
            names = ["auto"] + list(
                self.store.providers
            )
            if arg in names:
                self.provider_var.set(arg)
                self.append_chat(
                    "system",
                    f"Provider set to {arg}"
                )
            else:
                self.append_chat(
                    "system",
                    "Providers: " + ", ".join(names)
                )
            return True

        if cmd == "/route":
            provider, reason = self.router.decide(
                arg
            )
            self.append_chat(
                "system",
                f"{provider} ({reason})"
            )
            return True

        if cmd == "/apis":
            self.notebook.select(
                self.api_tab()
            )
            return True

        if cmd == "/logs":
            self.notebook.select(
                self.logs_tab()
            )
            return True

        return False

    def api_tab(self):
        return self.notebook.tabs().index(
            self.notebook.tabs()[
                1
            ]
        )

    def logs_tab(self):
        return self.notebook.tabs().index(
            self.notebook.tabs()[
                3
            ]
        )

    # ----------------------------------------------------------
    # API EDITOR
    # ----------------------------------------------------------

    def selected_provider_name(self) -> Optional[str]:
        selection = self.provider_list.curselection()
        if not selection:
            return None

        raw = self.provider_list.get(
            selection[0]
        )

        return raw[2:].strip()

    def load_selected_provider(self, _event=None):
        name = self.selected_provider_name()
        if not name:
            return

        cfg = self.store.providers[name]

        self.api_name_var.set(name)
        self.api_type_var.set(
            cfg.get("type", "openai_compatible")
        )
        self.api_enabled_var.set(
            bool(cfg.get("enabled", True))
        )

        if cfg.get("type") == "rest_json":
            self.api_url_var.set(
                cfg.get("url", "")
            )
        else:
            self.api_url_var.set(
                cfg.get("base_url", "")
            )

        self.api_endpoint_var.set(
            cfg.get(
                "endpoint",
                "/chat/completions"
            )
        )

        self.api_method_var.set(
            cfg.get("method", "POST")
        )

        self.api_model_var.set(
            cfg.get("model", "")
        )

        self.api_key_var.set(
            cfg.get("api_key", "")
        )

        self.api_key_env_var.set(
            cfg.get("api_key_env", "")
        )

        self.api_temp_var.set(
            str(cfg.get("temperature", 0.7))
        )

        self.api_max_tokens_var.set(
            str(cfg.get("max_tokens", 2048))
        )

        self.api_headers_text.delete(
            "1.0",
            "end"
        )
        self.api_headers_text.insert(
            "1.0",
            pretty_json(
                cfg.get("headers", {})
            )
        )

        body = (
            cfg.get("body_template", {})
            if cfg.get("type") == "rest_json"
            else cfg.get("extra_body", {})
        )

        self.api_body_text.delete(
            "1.0",
            "end"
        )
        self.api_body_text.insert(
            "1.0",
            pretty_json(body)
        )

        self.api_response_path_var.set(
            cfg.get("response_path", "")
        )

        self.update_api_editor_help()

    def add_provider(self):
        name = simpledialog.askstring(
            "Add API",
            "Provider name:",
            parent=self
        )

        if not name:
            return

        name = name.strip()

        if not re.match(
            r"^[A-Za-z0-9_.-]+$",
            name
        ):
            messagebox.showerror(
                "Invalid Name",
                "Use letters, digits, _, -, or ."
            )
            return

        if name in self.store.providers:
            messagebox.showerror(
                "Exists",
                "Provider already exists."
            )
            return

        self.store.providers[name] = {
            "type": "openai_compatible",
            "enabled": True,
            "base_url": "",
            "endpoint": "/chat/completions",
            "model": "",
            "api_key": "",
            "api_key_env": "",
            "headers": {
                "Content-Type": "application/json"
            },
            "temperature": 0.7,
            "max_tokens": 2048,
            "extra_body": {}
        }

        self.store.save()
        self.refresh_all()

        names = list(
            self.store.providers.keys()
        )
        idx = names.index(name)

        self.provider_list.selection_clear(
            0, "end"
        )
        self.provider_list.selection_set(
            idx
        )
        self.provider_list.event_generate(
            "<<ListboxSelect>>"
        )

    def delete_provider(self):
        name = self.selected_provider_name()

        if not name:
            return

        if not messagebox.askyesno(
            "Delete API",
            f"Delete provider '{name}'?"
        ):
            return

        self.store.providers.pop(
            name,
            None
        )

        if self.store.routing.get("default") == name:
            self.store.routing["default"] = ""

        self.store.routing["fallbacks"] = [
            x for x in self.store.routing.get(
                "fallbacks",
                []
            )
            if x != name
        ]

        self.store.routing["rules"] = [
            r for r in self.store.routing.get(
                "rules",
                []
            )
            if r.get("provider") != name
        ]

        self.store.save()
        self.refresh_all()

    def duplicate_provider(self):
        name = self.selected_provider_name()
        if not name:
            return

        new_name = simpledialog.askstring(
            "Duplicate API",
            "New provider name:",
            initialvalue=name + "_copy",
            parent=self
        )

        if not new_name:
            return

        if new_name in self.store.providers:
            messagebox.showerror(
                "Exists",
                "Provider already exists."
            )
            return

        self.store.providers[new_name] = deep_copy(
            self.store.providers[name]
        )

        self.store.save()
        self.refresh_all()

    def save_provider_editor(self):
        name = self.api_name_var.get().strip()
        ptype = self.api_type_var.get()

        if not name:
            messagebox.showerror(
                "Missing name",
                "Provider name is required."
            )
            return

        try:
            headers = json.loads(
                self.api_headers_text.get(
                    "1.0",
                    "end"
                ).strip() or "{}"
            )

            body = json.loads(
                self.api_body_text.get(
                    "1.0",
                    "end"
                ).strip() or "{}"
            )
        except Exception as exc:
            messagebox.showerror(
                "Invalid JSON",
                str(exc)
            )
            return

        old_name = self.selected_provider_name()

        cfg: Dict[str, Any] = {
            "type": ptype,
            "enabled": self.api_enabled_var.get(),
            "api_key": self.api_key_var.get(),
            "api_key_env": self.api_key_env_var.get(),
            "headers": headers,
        }

        if ptype == "openai_compatible":
            cfg.update({
                "base_url": self.api_url_var.get(),
                "endpoint": self.api_endpoint_var.get(),
                "model": self.api_model_var.get(),
                "temperature": float(
                    self.api_temp_var.get()
                ),
                "max_tokens": int(
                    self.api_max_tokens_var.get()
                ),
                "extra_body": body,
            })
        else:
            cfg.update({
                "url": self.api_url_var.get(),
                "method": self.api_method_var.get(),
                "body_template": body,
                "response_path": self.api_response_path_var.get(),
            })

        if (
            old_name
            and old_name != name
            and old_name in self.store.providers
        ):
            self.store.providers.pop(
                old_name
            )

            # Update routing references after rename.
            if self.store.routing.get("default") == old_name:
                self.store.routing["default"] = name

            self.store.routing["fallbacks"] = [
                name if x == old_name else x
                for x in self.store.routing.get(
                    "fallbacks",
                    []
                )
            ]

            for rule in self.store.routing.get("rules", []):
                if rule.get("provider") == old_name:
                    rule["provider"] = name

        self.store.providers[name] = cfg
        self.store.save()
        self.refresh_all()

        messagebox.showinfo(
            "Saved",
            f"Provider '{name}' saved."
        )

    def update_api_editor_help(self):
        ptype = self.api_type_var.get()

        if ptype == "openai_compatible":
            self.api_endpoint_label.grid()
            self.api_model_label.grid()
            self.api_temp_label.grid()
            self.api_max_tokens_label.grid()
            self.api_response_path_label.grid_remove()
            self.api_method_label.grid_remove()

            self.api_help.set(
                "OpenAI-compatible example:\n"
                "URL = https://host/v1\n"
                "Endpoint = /chat/completions\n"
                "The program sends JSON with model, messages, stream, "
                "temperature and max_tokens.\n\n"
                "API Key can be entered directly, or leave it blank and "
                "set an API Key Env Var such as OPENAI_API_KEY."
            )
        else:
            self.api_endpoint_label.grid_remove()
            self.api_model_label.grid_remove()
            self.api_temp_label.grid_remove()
            self.api_max_tokens_label.grid_remove()
            self.api_response_path_label.grid()
            self.api_method_label.grid()

            self.api_help.set(
                "Generic JSON mode:\n"
                "Body template placeholders:\n"
                "{{messages}}\n"
                "{{last_user_message}}\n"
                "{{conversation_json}}\n"
                "{{timestamp}}\n"
                "{{uuid}}\n\n"
                "Response Path is a dot path such as "
                "data.reply or choices.0.text."
            )

    def test_selected_provider(self):
        name = self.selected_provider_name()

        if not name:
            return

        self._test_provider_async(name)

    def _test_provider_async(self, name: str):
        def worker():
            try:
                result = self.providers.test(name)
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        "API Test",
                        f"Provider: {name}\n\n"
                        f"Response:\n{result.text[:5000]}"
                    )
                )
            except Exception as exc:
                self.after(
                    0,
                    lambda: messagebox.showerror(
                        "API Test Failed",
                        str(exc)
                    )
                )

        threading.Thread(
            target=worker,
            daemon=True
        ).start()

    # ----------------------------------------------------------
    # ROUTER
    # ----------------------------------------------------------

    def save_routing(self):
        self.store.routing["default"] = (
            self.route_default_var.get()
        )

        raw = self.route_fallback_var.get().strip()

        self.store.routing["fallbacks"] = [
            x.strip()
            for x in raw.split(",")
            if x.strip()
        ]

        self.store.save()
        self.refresh_routing()

    def add_rule(self):
        name = simpledialog.askstring(
            "Add Rule",
            "Rule name:",
            parent=self
        )

        if not name:
            return

        self.store.routing.setdefault(
            "rules",
            []
        ).append({
            "name": name,
            "provider": "",
            "contains_any": [],
            "contains_all": [],
            "regex": ""
        })

        self.store.save()
        self.refresh_routing()

        idx = len(
            self.store.routing["rules"]
        ) - 1

        self.rule_list.selection_clear(
            0, "end"
        )
        self.rule_list.selection_set(idx)
        self.rule_list.event_generate(
            "<<ListboxSelect>>"
        )

    def delete_rule(self):
        selection = self.rule_list.curselection()

        if not selection:
            return

        idx = selection[0]

        self.store.routing["rules"].pop(
            idx
        )

        self.store.save()
        self.refresh_routing()

    def load_selected_rule(self, _event=None):
        selection = self.rule_list.curselection()

        if not selection:
            return

        idx = selection[0]
        rules = self.store.routing.get(
            "rules",
            []
        )

        if idx >= len(rules):
            return

        rule = rules[idx]

        self.rule_name_var.set(
            rule.get("name", "")
        )
        self.rule_provider_var.set(
            rule.get("provider", "")
        )
        self.rule_any_var.set(
            ", ".join(
                rule.get("contains_any", [])
            )
        )
        self.rule_all_var.set(
            ", ".join(
                rule.get("contains_all", [])
            )
        )
        self.rule_regex_var.set(
            rule.get("regex", "")
        )

    def save_rules(self):
        selection = self.rule_list.curselection()

        if not selection:
            messagebox.showinfo(
                "Rules",
                "Select a rule first."
            )
            return

        idx = selection[0]

        def split_csv(value):
            return [
                x.strip()
                for x in value.split(",")
                if x.strip()
            ]

        self.store.routing["rules"][idx] = {
            "name": self.rule_name_var.get().strip(),
            "provider": self.rule_provider_var.get().strip(),
            "contains_any": split_csv(
                self.rule_any_var.get()
            ),
            "contains_all": split_csv(
                self.rule_all_var.get()
            ),
            "regex": self.rule_regex_var.get().strip()
        }

        self.store.save()
        self.refresh_routing()

    def test_route_text(self):
        text = self.route_test_var.get()

        provider, reason = self.router.decide(
            text
        )

        candidates = self.router.candidates(
            provider
        )

        self.route_test_result_var.set(
            f"Selected: {provider}\n"
            f"Reason: {reason}\n"
            f"Candidates: {', '.join(candidates)}"
        )

    def test_current_route(self):
        text = self.prompt_box.get(
            "1.0",
            "end"
        ).strip() or "test this message"

        provider, reason = self.router.decide(
            text,
            None if self.provider_var.get() == "auto"
            else self.provider_var.get()
        )

        messagebox.showinfo(
            "Route",
            f"Provider: {provider}\nReason: {reason}"
        )

    # ----------------------------------------------------------
    # LOGS
    # ----------------------------------------------------------

    def show_selected_log(self, _event=None):
        selection = self.log_tree.selection()

        if not selection:
            return

        iid = selection[0]

        try:
            idx = int(iid)
        except Exception:
            return

        if idx >= len(
            getattr(self, "_all_visible_logs", [])
        ):
            return

        item = self._all_visible_logs[idx]

        self.log_detail.delete(
            "1.0",
            "end"
        )
        self.log_detail.insert(
            "1.0",
            pretty_json(item)
        )

    def export_logs(self):
        path = filedialog.asksaveasfilename(
            title="Export Logs",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")]
        )

        if not path:
            return

        save_json(
            Path(path),
            self.logger.read_all()
        )

    def clear_logs(self):
        if not messagebox.askyesno(
            "Clear Logs",
            "Delete all local logs?"
        ):
            return

        self.logger.clear()
        self.refresh_logs()

    # ----------------------------------------------------------
    # TOOL PLAYGROUND
    # ----------------------------------------------------------

    def open_tool_playground(self):
        win = tk.Toplevel(self)
        win.title("Tool Playground")
        win.geometry("850x600")

        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")

        tool_var = tk.StringVar(
            value="calculator"
        )

        ttk.Label(
            top,
            text="Tool"
        ).pack(side="left")

        ttk.Combobox(
            top,
            textvariable=tool_var,
            state="readonly",
            values=[
                "calculator",
                "current_time",
                "make_uuid",
                "sha256",
                "base64_encode",
                "base64_decode",
                "url_encode",
                "url_decode",
                "json_pretty",
                "http_get",
                "run_command"
            ],
            width=22
        ).pack(
            side="left",
            padx=8
        )

        input_box = tk.Text(
            win,
            height=8,
            wrap="word",
            font=("Consolas", 10)
        )
        input_box.pack(
            fill="x",
            padx=8,
            pady=8
        )

        output = tk.Text(
            win,
            wrap="word",
            font=("Consolas", 10)
        )
        output.pack(
            fill="both",
            expand=True,
            padx=8,
            pady=(0, 8)
        )

        def run():
            name = tool_var.get()
            arg = input_box.get(
                "1.0",
                "end"
            ).strip()

            try:
                fn = getattr(Tools, name)

                if name in {
                    "current_time",
                    "make_uuid"
                }:
                    value = fn()
                elif name == "calculator":
                    value = fn(arg)
                else:
                    value = fn(arg)

                output.delete(
                    "1.0",
                    "end"
                )
                output.insert(
                    "1.0",
                    str(value)
                )

            except Exception as exc:
                output.delete(
                    "1.0",
                    "end"
                )
                output.insert(
                    "1.0",
                    traceback.format_exc()
                )

        ttk.Button(
            top,
            text="RUN",
            command=run
        ).pack(side="left")

    # ----------------------------------------------------------
    # SETTINGS / SAVE
    # ----------------------------------------------------------

    def save_settings(self):
        try:
            history = int(
                self.settings_history_var.get()
            )
            timeout = int(
                self.settings_timeout_var.get()
            )
        except ValueError:
            messagebox.showerror(
                "Settings",
                "History and timeout must be integers."
            )
            return

        self.store.settings.update({
            "system_prompt": self.settings_prompt.get(
                "1.0",
                "end"
            ).strip(),
            "history_limit": history,
            "timeout": timeout,
            "verify_ssl": self.settings_verify_var.get(),
            "stream": self.settings_stream_var.get(),
            "autosave": self.settings_autosave_var.get(),
        })

        self.trim_history()
        self.persist_current_chat()
        self.store.save()

        messagebox.showinfo(
            "Settings",
            "Settings saved."
        )

    def save_everything(self):
        self.save_settings_silent()
        self.store.save()
        self.chat_status_var.set(
            "Everything saved."
        )

    def save_settings_silent(self):
        try:
            self.store.settings.update({
                "system_prompt": self.settings_prompt.get(
                    "1.0",
                    "end"
                ).strip(),
                "history_limit": int(
                    self.settings_history_var.get()
                ),
                "timeout": int(
                    self.settings_timeout_var.get()
                ),
                "verify_ssl": self.settings_verify_var.get(),
                "stream": self.settings_stream_var.get(),
                "autosave": self.settings_autosave_var.get(),
            })
        except Exception:
            pass

    def on_close(self):
        self.save_settings_silent()
        self.persist_current_chat()
        self.store.save()
        self.destroy()


# ==============================================================



# ============================================================
# GRAPH UI INTEGRATION FOR THE EXISTING APP
# ============================================================

def _graph_build_tab(self):
    tab = ttk.Frame(self.notebook, padding=8)
    self.graphs_tab = tab
    self.notebook.add(tab, text="Agents / Graphs")

    top = ttk.Frame(tab)
    top.pack(fill="x")

    ttk.Label(
        top,
        text="Visual Agents / Graphs",
        style="Heading.TLabel"
    ).pack(side="left")

    buttons = [
        ("New", self.graph_new),
        ("Open Designer", self.graph_open),
        ("Run", self.graph_run),
        ("Duplicate", self.graph_duplicate),
        ("Delete", self.graph_delete),
        ("Import JSON", self.graph_import),
        ("Export JSON", self.graph_export),
    ]

    for text, cmd in buttons:
        ttk.Button(
            top,
            text=text,
            command=cmd
        ).pack(side="left", padx=2)

    pane = ttk.PanedWindow(
        tab,
        orient="horizontal"
    )
    pane.pack(fill="both", expand=True, pady=8)

    left = ttk.Frame(pane, padding=6)
    right = ttk.Frame(pane, padding=6)

    pane.add(left, weight=1)
    pane.add(right, weight=3)

    self.graph_list = tk.Listbox(
        left,
        exportselection=False
    )
    self.graph_list.pack(
        fill="both",
        expand=True
    )
    self.graph_list.bind(
        "<<ListboxSelect>>",
        self.graph_show
    )

    self.graph_summary = tk.Text(
        right,
        font=("Consolas", 9),
        wrap="word"
    )
    self.graph_summary.pack(
        fill="both",
        expand=True
    )

    ttk.Label(
        right,
        text=(
            "Create arbitrary graph-shaped agents. Nodes can call an existing "
            "API provider, perform HTTP requests, execute Python, execute "
            "commands, parse JSON, branch, loop, iterate over lists, ask the "
            "user, and invoke another graph. Multiple outgoing arrows are "
            "supported; edges can carry branch labels."
        ),
        wraplength=900,
        justify="left"
    ).pack(
        anchor="w",
        pady=6
    )


def _graph_refresh(self):
    if not hasattr(self, "graph_list"):
        return

    self.graph_list.delete(0, "end")

    for name, graph in self.store.data.setdefault(
        "workflows",
        {}
    ).items():
        self.graph_list.insert(
            "end",
            f"{name} ({len(graph.get('nodes', []))} nodes)"
        )


def _graph_selected(self):
    sel = self.graph_list.curselection()
    if not sel:
        return None
    return self.graph_list.get(
        sel[0]
    ).rsplit(" (", 1)[0]


def _graph_show(self, _event=None):
    self.graph_summary.delete(
        "1.0",
        "end"
    )

    name = self.graph_selected()
    if not name:
        return

    graph = self.store.data["workflows"][name]

    self.graph_summary.insert(
        "1.0",
        f"NAME: {name}\n\n"
        f"START: {graph.get('start')}\n"
        f"NODES: {len(graph.get('nodes', []))}\n"
        f"EDGES: {len(graph.get('edges', []))}\n\n"
        "NODES\n" +
        "\n".join(
            f"  {n['id']} [{n.get('type')}]"
            for n in graph.get("nodes", [])
        ) +
        "\n\nEDGES\n" +
        "\n".join(
            f"  {e.get('from')} -> {e.get('to')} "
            f"[{e.get('condition', 'default')}]"
            + (
                f" expr={e['expression']}"
                if e.get("expression")
                else ""
            )
            for e in graph.get("edges", [])
        )
    )


def _graph_new(self):
    name = simpledialog.askstring(
        "New Agent Graph",
        "Graph name:",
        parent=self
    )
    if not name:
        return

    name = name.strip()

    if name in self.store.data.setdefault(
        "workflows",
        {}
    ):
        messagebox.showerror(
            "Graph",
            "That graph already exists.",
            parent=self
        )
        return

    self.store.data["workflows"][name] = {
        "start": "start",
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "x": 120,
                "y": 220,
                "config": {}
            },
            {
                "id": "end",
                "type": "end",
                "x": 520,
                "y": 220,
                "config": {}
            }
        ],
        "edges": [
            {
                "from": "start",
                "to": "end",
                "condition": "default"
            }
        ]
    }

    self.store.save()
    self.graph_refresh()


def _graph_open(self):
    name = self.graph_selected()
    if name:
        GraphDesigner(
            self,
            name
        )


def _graph_run(self):
    name = self.graph_selected()
    if not name:
        return

    variables = {
        "last_user_message":
            self.prompt_box.get("1.0", "end").strip(),
        "workflow_provider": (
            self.provider_var.get()
            if self.provider_var.get() != "auto"
            else self.store.routing.get(
                "default",
                ""
            )
        ),
        "chat_name": self.current_chat
    }

    win = tk.Toplevel(self)
    win.title(
        f"Agent Run - {name}"
    )
    win.geometry(
        "850x650"
    )

    out = tk.Text(
        win,
        font=("Consolas", 9),
        wrap="word"
    )
    out.pack(
        fill="both",
        expand=True
    )

    def emit(line):
        self.after(
            0,
            lambda: (
                out.insert(
                    "end",
                    str(line) + "\n"
                ),
                out.see("end")
            )
        )

    def worker():
        try:
            result = self.graph_engine.run(
                name,
                variables=variables,
                emit=emit
            )

            self.after(
                0,
                lambda: out.insert(
                    "end",
                    "\nFINAL VARIABLES\n"
                    + pretty(result)
                )
            )
        except Exception:
            error = traceback.format_exc()
            self.after(
                0,
                lambda: out.insert(
                    "end",
                    "\nERROR\n"
                    + error
                )
            )

    threading.Thread(
        target=worker,
        daemon=True
    ).start()


def _graph_duplicate(self):
    name = self.graph_selected()
    if not name:
        return

    new = simpledialog.askstring(
        "Duplicate Graph",
        "New name:",
        initialvalue=name + "_copy",
        parent=self
    )

    if not new:
        return

    if new in self.store.data.setdefault(
        "workflows",
        {}
    ):
        messagebox.showerror(
            "Graph",
            "Already exists.",
            parent=self
        )
        return

    self.store.data["workflows"][new] = clone(
        self.store.data["workflows"][name]
    )
    self.store.save()
    self.graph_refresh()


def _graph_delete(self):
    name = self.graph_selected()
    if not name:
        return

    if not messagebox.askyesno(
        "Delete Graph",
        f"Delete '{name}'?",
        parent=self
    ):
        return

    del self.store.data["workflows"][name]
    self.store.save()
    self.graph_refresh()


def _graph_import(self):
    path = filedialog.askopenfilename(
        title="Import agent graph JSON",
        filetypes=[("JSON", "*.json")]
    )
    if not path:
        return

    try:
        data = load_json(
            Path(path),
            {}
        )

        if "nodes" not in data or "edges" not in data:
            if "graph" in data:
                data = data["graph"]
            else:
                raise ValueError(
                    "JSON does not look like an agent graph."
                )

        name = data.get(
            "name",
            Path(path).stem
        )

        graph = data.get(
            "graph",
            data
        )

        self.store.data.setdefault(
            "workflows",
            {}
        )[name] = graph

        self.store.save()
        self.graph_refresh()

        messagebox.showinfo(
            "Import",
            f"Imported graph '{name}'.",
            parent=self
        )

    except Exception as exc:
        messagebox.showerror(
            "Import",
            str(exc),
            parent=self
        )


def _graph_export(self):
    name = self.graph_selected()
    if not name:
        return

    path = filedialog.asksaveasfilename(
        title="Export agent graph JSON",
        defaultextension=".json",
        filetypes=[("JSON", "*.json")]
    )

    if not path:
        return

    save_json(
        Path(path),
        {
            "name": name,
            "graph": self.store.data[
                "workflows"
            ][name]
        }
    )


def _graph_build_wrapper(self):
    # Give the graph engine access to the current application.
    self.graph_engine = GraphEngine(self)
    self.workflow_current_name = ""

    # The existing v2 uses `self.providers` as ProviderManager.
    # GraphEngine expects `self.providers`, so no extra manager needed.

    _old_build(self)


def _graph_refresh_wrapper(self):
    _old_refresh(self)
    self.graph_refresh()


# Keep references to original App methods.
_old_build = App._build
_old_refresh = App.refresh_all

# Patch the lifecycle.
App._build = _graph_build_wrapper
App.refresh_all = _graph_refresh_wrapper

# Expose graph methods on App.
App._build_workflows_tab = _graph_build_tab
App.graph_refresh = _graph_refresh
App.graph_selected = _graph_selected
App.graph_show = _graph_show
App.graph_new = _graph_new
App.graph_open = _graph_open
App.graph_run = _graph_run
App.graph_duplicate = _graph_duplicate
App.graph_delete = _graph_delete
App.graph_import = _graph_import
App.graph_export = _graph_export

# Patch the build method one more time so the graph tab is appended after
# the original tabs have been constructed.
def _build_with_graphs(self):
    _graph_build_wrapper(self)
    self._build_workflows_tab()

App._build = _build_with_graphs

# Seed templates after the graph engine is in place.
def _graph_seed_templates(self):
    workflows = self.store.data.setdefault(
        "workflows",
        {}
    )
    for n, g in GRAPH_TEMPLATES.items():
        workflows.setdefault(n, deep_copy(g))

_old_init = App.__init__

def _new_init(self):
    _old_init(self)
    _graph_seed_templates(self)
    self.store.save()

App.__init__ = _new_init


# ============================================================
# BOOT
# ============================================================

# ==============================================================

def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
