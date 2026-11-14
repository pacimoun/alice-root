"""Explicit SoundCloud voice commands; no transcript or account-token logging.

Receives only prefixed requests from the station's existing command agent.
Playback stays in the independently authenticated SoundCloud browser profile.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import stat
import time
import unicodedata

VERSION = "0.1.1"
MARKERS = ("музыка",)


class MusicError(Exception):
    pass


def normalize(value):
    value = unicodedata.normalize("NFC", value).casefold().replace("ё", "е")
    return re.sub(r"[^a-z0-9а-я]+", " ", value).strip()


def parse_spoken(text):
    if not isinstance(text, str) or not 1 <= len(text.encode("utf-8")) <= 1024 or any(ord(c) < 32 for c in text):
        raise MusicError("INVALID_MUSIC_COMMAND")
    # Keep the original query for search: punctuation/case belong to its title.
    body = re.sub(r"^\s*алиса\b[\s,.:;!?—–-]*", "", text, flags=re.I).strip()
    for marker in MARKERS:
        match = re.match(r"^"+re.escape(marker)+r"\b[\s,.:;!?—–-]+(.+)$", body, flags=re.I)
        if match:
            body = match[1].strip()
            break
    else:
        raise MusicError("MUSIC_PREFIX_REQUIRED")
    body = re.sub(r"[\s,.:;!?—–-]+пожалуйста[\s.!?]*$", "", body, flags=re.I).strip()
    phrase = normalize(body)
    commands = {
        "likes": {"лайки", "мои лайки", "любимые", "любимые треки", "мои любимые треки", "включи лайки", "включи мои лайки", "включи любимые треки", "включи мои любимые треки"},
        "pause": {"пауза", "стоп", "останови", "останови музыку", "выключи", "выключи музыку", "поставь на паузу"},
        "resume": {"продолжи", "продолжай", "продолжи музыку", "играй", "возобнови", "сними с паузы"},
        "next": {"следующий", "следующая", "следующий трек", "следующая песня", "дальше", "переключи трек"},
        "previous": {"предыдущий", "предыдущая", "предыдущий трек", "предыдущая песня", "назад"},
    }
    for action, phrases in commands.items():
        if phrase in phrases:
            return action, None
    match = re.match(r"^(?:включи|найди|сыграй|поставь)(?:\s+трек|\s+песню)?\s+(.+)$", body, flags=re.I)
    if match:
        query = match[1].strip()
        if 1 <= len(query) <= 300 and normalize(query):
            return "search", query
    raise MusicError("UNKNOWN_MUSIC_COMMAND")


def paths():
    home = Path.home()
    state = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
    return (Path(os.environ.get("ALICE_SC_CONTROL_DIR", state / "verity/soundcloud-control")) / "control.sock",
            Path(os.environ.get("ALICE_SC_LAUNCHER", home / ".local/bin/verity-sc")),
            state / "verity/soundcloud-voice/gui-env.json")


async def rpc(action, query=None):
    sock, _, _ = paths()
    try:
        info = sock.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise MusicError("UNSAFE_CONTROL_SOCKET")
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(sock)), 3)
    except (FileNotFoundError, ConnectionRefusedError):
        raise MusicError("CONTROLLER_NOT_RUNNING") from None
    try:
        writer.write(json.dumps({"action": action, "value": query}, ensure_ascii=False).encode()+b"\n")
        await asyncio.wait_for(writer.drain(), 3)
        raw = await asyncio.wait_for(reader.readline(), 90)
        if not raw or len(raw) > 65536:
            raise MusicError("INVALID_CONTROL_REPLY")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise MusicError("INVALID_CONTROL_REPLY")
        if result.get("ok") is not True:
            # Server errors are fixed codes; never retain an arbitrary page or
            # browser error string in the bridge's persistent status.
            code = result.get("error", "PLAYBACK_FAILED")
            raise MusicError(code if isinstance(code, str) and re.fullmatch(r"[A-Z_]{1,80}", code) else "PLAYBACK_FAILED")
        return result
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def browser_environment():
    environment = dict(os.environ)
    if environment.get("DISPLAY") or environment.get("WAYLAND_DISPLAY"):
        return environment
    _, _, config = paths()
    try:
        info = config.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise MusicError("UNSAFE_GUI_ENVIRONMENT")
        data = json.loads(config.read_text())
        allowed = {"DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY", "XDG_SESSION_TYPE"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise MusicError("INVALID_GUI_ENVIRONMENT")
        for key, value in data.items():
            if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
                raise MusicError("INVALID_GUI_ENVIRONMENT")
            environment.setdefault(key, value)
    except (FileNotFoundError, ValueError):
        pass
    return environment


async def ensure_running():
    _, launcher, _ = paths()
    proc = await asyncio.create_subprocess_exec(str(launcher), "start", env=browser_environment(),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), 40)
        if len(stdout) > 65536:
            raise MusicError("CONTROLLER_START_FAILED")
        try:
            result = json.loads(stdout)
        except ValueError:
            result = {}
        if proc.returncode or not isinstance(result, dict) or result.get("running") is not True:
            raise MusicError("CONTROLLER_START_FAILED")
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), 3)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


class Music:
    def __init__(self, status, save):
        self.status, self.save = status, save
        self.task = None
        status["soundcloud_voice"] = {"version": VERSION, "phase": "ready", "received": 0, "completed": 0}

    def submit(self, event):
        state = self.status["soundcloud_voice"]
        try:
            action, query = parse_spoken(event.get("text"))
        except (MusicError, UnicodeError):
            state["error"] = "UNKNOWN_MUSIC_COMMAND"
            self.save()
            return
        if self.task and not self.task.done():
            state["busy_commands"] = state.get("busy_commands", 0) + 1
            self.save()
            return
        state.update(phase="working", action=action, received=state["received"] + 1, at=time.time())
        state.pop("error", None)
        self.save()
        self.task = asyncio.create_task(self._run(action, query))

    async def _run(self, action, query):
        state = self.status["soundcloud_voice"]
        try:
            try:
                await rpc(action, query)
            except MusicError as error:
                if str(error) != "CONTROLLER_NOT_RUNNING" or action not in {"likes", "search", "resume"}:
                    raise
                await ensure_running()
                await rpc(action, query)
            state["completed"] += 1
            state["phase"] = "ready"
            state.pop("error", None)
        except asyncio.CancelledError:
            state["phase"] = "stopped"
            raise
        except MusicError as error:
            state.update(phase="error", error=str(error))
        except Exception:
            state.update(phase="error", error="MUSIC_CONTROL_UNAVAILABLE")
        finally:
            state["at"] = time.time()
            self.save()

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
