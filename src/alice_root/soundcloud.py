#!/usr/bin/env python3
"""SoundCloud: отдельный браузер, приватный DevTools pipe, Bluetooth.

Контроллер браузера на Python stdlib; запуск без sudo.

Управляет собственным Chromium-профилем через приватный DevTools pipe.
Использует вход владельца в SoundCloud и доступ его подписки; звук идёт по Bluetooth.
Установка и удаление выполняются общей командой проекта alice-root.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.parse import quote, urlsplit

VERSION = "0.1.3"
MARKER = "verity-soundcloud-control-v1"
MAX_MESSAGE = 65536
ROUTES = {"status", "play", "search", "likes", "pause", "resume", "next", "previous", "diagnose", "stop"}


class Failure(Exception):
    pass


def configuration():
    config = Path(os.environ.get("ALICE_ROOT_CONFIG", str(Path(os.environ.get("XDG_CONFIG_HOME", Path.home()/".config"))/"alice-root/config.toml"))).expanduser()
    if not config.exists():
        return {}
    try:
        with config.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, ValueError):
        raise Failure("INVALID_CONFIG: проверь локальный TOML") from None


def locations():
    config = configuration()
    paths, music = config.get("paths", {}), config.get("soundcloud", {})
    base = Path(paths.get("state", str(Path(os.environ.get("XDG_STATE_HOME", Path.home()/".local/state"))/"alice-root"))).expanduser()
    state = Path(os.environ.get("ALICE_SC_CONTROL_DIR", music.get("state", str(base/"soundcloud-control")))).expanduser()
    profile = Path(music.get("profile", str(base/"soundcloud-profile"))).expanduser()
    for item in (profile, state, *(state/name for name in ("control.sock", "controller.lock", "controller.pid", "browser.log", "controller.log", "last-error.json"))):
        if not item.is_absolute():
            raise Failure("ABSOLUTE_PATH_REQUIRED")
        for part in (item, *item.parents):
            if part.is_symlink():
                raise Failure("STOP_SYMLINK")
        if item in (profile, state) and item.exists():
            if not item.is_dir() or item.stat().st_uid != os.getuid() or item.stat().st_mode & 0o077:
                raise Failure("PRIVATE_OWNED_DIRECTORY_REQUIRED")
    return {"profile": profile, "state": state, "socket": state/"control.sock"}


def atomic(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise Failure("STOP_SYMLINK: " + str(path))
    fd, temporary = tempfile.mkstemp(prefix=".verity-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode() + b"\n"


def validate(action, value=None):
    if not isinstance(action, str) or action not in ROUTES:
        raise Failure("UNKNOWN_ACTION")
    if action == "play":
        if not isinstance(value, str) or len(value) > 1200:
            raise Failure("INVALID_TRACK_URL")
        parsed = urlsplit(value)
        parts = parsed.path.strip("/").split("/")
        if (parsed.scheme != "https" or parsed.netloc != "soundcloud.com" or parsed.query
                or parsed.fragment or len(parts) != 2
                or not all(re.fullmatch(r"[A-Za-z0-9_-]{1,300}", item) for item in parts)
                or parts[0] in {"you", "search", "discover", "stream", "settings", "pages", "terms-of-use", "oauth", "oauth2", "login", "logout", "signin", "register", "upload"}
                or any(ord(c) < 32 for c in value)):
            raise Failure("INVALID_TRACK_URL: нужен обычный https://soundcloud.com/исполнитель/трек")
    elif action == "search":
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 300 or any(ord(c) < 32 for c in value):
            raise Failure("INVALID_SEARCH")
        try: value.encode("utf-8")
        except UnicodeError: raise Failure("INVALID_SEARCH") from None
        value = value.strip()
    elif value is not None:
        raise Failure("UNEXPECTED_ARGUMENT")
    return value


def profile_process(profile):
    """Recognize the exact dedicated profile, including flattened cmdline."""
    profile = Path(profile).resolve()
    try:
        pid = int(os.readlink(profile / "SingletonLock").rsplit("-", 1)[1])
        proc = Path("/proc") / str(pid)
        if proc.stat().st_uid != os.getuid():
            return None
        if (proc / "exe").resolve(strict=True).name not in {"chromium", "chromium-browser", "chrome"}:
            return None
        raw = (proc / "cmdline").read_bytes().replace(b"\0", b" ")
        marker = b"--user-data-dir=" + os.fsencode(profile)
        if re.search(rb"(?:^|\s)" + re.escape(marker) + rb"(?=\s|$)", raw) and not re.search(rb"(?:^|\s)--type=", raw):
            return pid
    except (OSError, ValueError):
        pass
    return None


def bluetooth_sink():
    try:
        result = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=8, check=True)
    except (OSError, subprocess.SubprocessError):
        raise Failure("PIPEWIRE_UNAVAILABLE") from None
    mac = configuration().get("station", {}).get("bluetooth_mac", "")
    if not isinstance(mac, str) or not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
        raise Failure("BLUETOOTH_MAC_REQUIRED: заполни station.bluetooth_mac в локальном TOML")
    prefix = "bluez_output." + mac.replace(":", "_")
    matches = [line.split()[1] for line in result.stdout.splitlines()
               if len(line.split()) > 1 and line.split()[1].casefold().startswith((prefix + ".").casefold())]
    if len(matches) != 1:
        raise Failure("BT_NOT_CONNECTED: подключи колонку к Fedora по Bluetooth")
    return matches[0]


PAGE = r"""
(request => {
  if (location.origin !== 'https://soundcloud.com') return {ok:false, code:'PAGE_LOADING'};
  const q = (s, root=document) => root.querySelector(s);
  const all = (s, root=document) => Array.from(root.querySelectorAll(s));
  const clean = x => String(x || '').replace(/\s+/g, ' ').trim().slice(0, 250);
  const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const enabled = el => !!el && !el.disabled && el.getAttribute('aria-disabled') !== 'true' && !el.classList.contains('sc-button-disabled');
  const available = el => visible(el) && enabled(el);
  const label = el => clean(el?.getAttribute('aria-label') || el?.getAttribute('title') || el?.textContent);
  const player = q('.playControls') || q('[class*=MiniPlayer_MiniPlayer]');
  const playControl = q('.playControl, .playControls__play, [data-testid="miniplayer-play"], [data-testid="miniplayer-pause"]');
  const text = selectors => { for (const selector of selectors) { const el=q(selector); if (el && clean(el.textContent)) return clean(el.textContent); } return null; };
  const trackURL = raw => {
    try { const u=new URL(raw,location.origin); const p=u.pathname.split('/').filter(Boolean);
      return u.origin===location.origin && p.length===2 && !['you','search','discover','settings','pages'].includes(p[0]) ? u.origin+u.pathname : null;
    } catch { return null; }
  };
  const trackLink = q('.playbackSoundBadge__titleLink, [class*=MiniPlayer_MiniPlayerTrack] a');
  let playing = null;
  if (playControl) playing = playControl.classList.contains('playing') || playControl.dataset.testid==='miniplayer-pause' || /^pause|^приостанов|^пауза/i.test(label(playControl));
  if (navigator.mediaSession?.playbackState === 'playing') playing=true;
  if (all('audio,video').some(x=>!x.paused && !x.ended && x.currentTime>0)) playing=true;
  const account = q('.userNav__username, .header__userNav .userNav__avatar, [data-testid="user-menu"]');
  const signIn = all('header button, header a, .header button, .header a').some(x=>/^(sign in|log in|войти)$/i.test(label(x)));
  const snapshot = {
    page: location.origin+location.pathname,
    ready: document.readyState==='complete',
    signed_in: account ? true : signIn ? false : null,
    playing,
    track: text(['.playbackSoundBadge__titleLink > span:nth-child(2)', '.playbackSoundBadge__titleLink', '[class*=MiniPlayer_MiniPlayerTrack]']),
    artist: text(['.playbackSoundBadge__titleContextContainer > a', '.playbackSoundBadge__lightLink', '[class*=MiniPlayer_MiniPlayerArtist]']),
    track_url: trackLink ? trackURL(trackLink.href) : null
  };
  const reply = extras => Object.assign({ok:true},snapshot,extras||{});
  const cardSelector = '.soundList__item,.trackItem,.sound,.audibleTile,.playableTile';
  const outsideList = el => el.closest('.playControls,[class*=MiniPlayer_MiniPlayer],header,.header,aside,.l-sidebar,.sidebarModule');
  const cards = () => all(cardSelector).filter(x=>!outsideList(x));
  const cardLink = item => q('a.soundTitle__title[href],a.trackItem__trackTitle[href]',item)
    || q('a.audibleTile__audibleHeading[href],a.playableTile__heading[href]',item)
    || q('a.audibleTile__artworkLink[href],a.playableTile__artworkLink[href]',item);
  const cardButton = item => q('.playButton,.sc-button-play,[data-testid="play-button"]',item);
  const buttonInfo = button => button ? {label:label(button),classes:clean(button.className),
    testid:button.getAttribute('data-testid')||button.getAttribute('data-test-id')||null,
    visible:visible(button),enabled:enabled(button)} : null;
  if (request.op==='status') return reply();
  if (request.op==='diagnose') {
    const seen = new Set();
    // Bounded layout clues only: no HTML, cookies, storage or account headers.
    const links=all('a[href]').filter(x=>{
      const url=trackURL(x.href);
      if (!url || outsideList(x) || !visible(x) || seen.has(url)) return false;
      seen.add(url); return true;
    }).slice(0,6).map(x=>({url:trackURL(x.href),text:clean(x.textContent),classes:clean(x.className),
      parents:[x.parentElement,x.parentElement?.parentElement,x.parentElement?.parentElement?.parentElement]
        .filter(Boolean).map(p=>clean(p.className))}));
    return reply({controls:all('.playControls button,.playControls [role=button],.playButton,.sc-button-play,[data-testid*=miniplayer]')
        .filter(visible).slice(0,14).map(buttonInfo),
      list_items:all('.soundList__item,.trackItem').length,
      tiles:all('.audibleTile,.playableTile').length,
      track_cards:cards().filter(visible).slice(0,6).map(item=>({classes:clean(item.className),
        url:trackURL(cardLink(item)?.href),play_button:buttonInfo(cardButton(item))})),
      track_links:links,page_title:clean(document.title)});
  }
  if (['pause','resume','next','previous'].includes(request.op)) {
    if (request.op==='pause' && playing===false || request.op==='resume' && playing===true) return reply({clicked:false});
    let button=playControl;
    if (request.op==='next') button=q('.skipControl__next,.playControls__next,[data-testid="miniplayer-next"]');
    if (request.op==='previous') button=q('.skipControl__previous,.playControls__prev,[data-testid="miniplayer-previous"]');
    if (!available(button)) return {ok:false,code:'PLAYER_CONTROL_NOT_FOUND'};
    button.click(); return reply({clicked:true});
  }
  if (request.op==='play-track') {
    const hero=q('.fullListenHero,.fullHero,[class*=TrackPage]') || document;
    const button=q('.playButton,.sc-button-play,[data-testid="play-button"]',hero);
    if (!available(button)) return {ok:false,code:'TRACK_PLAY_BUTTON_NOT_FOUND'};
    const already=button.classList.contains('sc-button-pause') || /^pause|^пауза/i.test(label(button));
    if (!already) button.click();
    return reply({clicked:!already,selected_url:location.origin+location.pathname});
  }
  if (request.op==='play-first') {
    if (!location.pathname.startsWith('/search/') && location.pathname!=='/you/likes') return {ok:false,code:'UNEXPECTED_PAGE'};
    for (const item of cards()) {
      if (!visible(item) || item.classList.contains('playlist') || item.getAttribute('data-playButton')==='never') continue;
      const link=cardLink(item);
      const url=link && trackURL(link.href);
      if (!url) continue;
      // SoundCloud tiles prefetch track policies on mouseenter and hide their
      // play button until hover. Reveal the view before selecting its button.
      item.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));
      const button=cardButton(item);
      const hoverSlot=button?.closest('.audibleTile__playButton,.playableTile__playButton,.trackItem__play');
      const hoverOnly=!!hoverSlot && item.contains(hoverSlot);
      if (!enabled(button) || !visible(button) && !hoverOnly) continue;
      const already=button.classList.contains('sc-button-pause') || /^pause|^пауза/i.test(label(button));
      if (!already) button.click();
      return reply({clicked:!already,selected_url:url,selected_title:clean(link.textContent)});
    }
    return {ok:false,code:'TRACK_LIST_NOT_READY'};
  }
  return {ok:false,code:'UNKNOWN_PAGE_ACTION'};
})
"""


def display_dependencies(window=False):
    if window:
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            raise Failure("DESKTOP_REQUIRED: выполни window из терминала графического сеанса Fedora")
    elif not shutil.which("Xvfb") or not shutil.which("xauth"):
        raise Failure("BACKGROUND_TOOLS_MISSING: нужны уже установленные Xvfb и xauth")


class VirtualDisplay:
    """One private, authenticated Xvfb; the GPT display is managed separately."""
    def __init__(self, log, binary=None):
        self.log = Path(log)
        self.binary = binary or shutil.which("Xvfb")
        self.proc = None
        self.name = None
        self.authority = None
        self.temporary = None
        self.number = None

    def socket_path(self, number):
        return Path(f"/tmp/.X11-unix/X{number}")

    async def start(self):
        if not self.binary or not shutil.which("xauth"):
            raise Failure("BACKGROUND_TOOLS_MISSING")
        # The existing GPT bridge uses 91..190. Do not touch another display's
        # sockets, lock files, authority, process, or browser profile.
        self.number = next((n for n in range(201, 301)
            if not Path(f"/tmp/.X{n}-lock").exists() and not self.socket_path(n).exists()), None)
        if self.number is None:
            raise Failure("NO_FREE_SOUNDCLOUD_DISPLAY")
        self.name = f":{self.number}"
        try:
            self.temporary = tempfile.TemporaryDirectory(prefix="xvfb-", dir=self.log.parent)
            self.authority = Path(self.temporary.name) / "authority"
            self.authority.touch(mode=0o600, exist_ok=False)
            result = subprocess.run(["xauth", "-f", str(self.authority)],
                input=f"add {self.name} . {secrets.token_hex(16)}\n".encode(),
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=8)
            if result.returncode:
                raise Failure("DISPLAY_AUTHORIZATION_FAILED")
            with self.log.open("ab") as log:
                self.proc = subprocess.Popen([self.binary, self.name, "-screen", "0", "1280x800x24",
                    "-nolisten", "tcp", "-auth", str(self.authority), "-noreset"],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            for _ in range(50):
                if self.proc.poll() is not None:
                    raise Failure("VIRTUAL_DISPLAY_START_FAILED")
                if self.socket_path(self.number).exists():
                    return
                await asyncio.sleep(.1)
            raise Failure("VIRTUAL_DISPLAY_TIMEOUT")
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.proc:
            if self.proc.poll() is None:
                with contextlib.suppress(ProcessLookupError): self.proc.terminate()
                for _ in range(30):
                    if self.proc.poll() is not None: break
                    await asyncio.sleep(.1)
                if self.proc.poll() is None:
                    with contextlib.suppress(ProcessLookupError): self.proc.kill()
            self.proc.wait(timeout=3)
            self.proc = None
        if self.temporary:
            self.temporary.cleanup()
            self.temporary = None
        self.name = self.authority = self.number = None


class Browser:
    def __init__(self, profile, log, sink, binary=None, extra_args=(), background=True, display=None):
        self.profile, self.log, self.sink = Path(profile), Path(log), sink
        self.binary = binary or shutil.which("chromium-browser") or shutil.which("chromium")
        self.extra_args = list(extra_args)
        self.proc = None
        self.pending = {}
        self.counter = 0
        self.closed = asyncio.Event()
        self.session = None
        self.writer_fd = None
        self.reader_task = None
        self.read_transport = None
        self.background = background
        self.display = display or VirtualDisplay(self.log)
        self.display_task = None

    async def start(self):
        if not self.binary:
            raise Failure("CHROMIUM_NOT_FOUND")
        if profile_process(self.profile):
            raise Failure("PROFILE_IN_USE: закрой прежнее окно SoundCloud в отдельном Chromium")
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.background:
            await self.display.start()
        # FD 3 receives DevTools requests; FD 4 carries browser replies.
        parent_read, child_write = os.pipe()
        child_read, parent_write = os.pipe()
        child_input = fcntl.fcntl(child_read, fcntl.F_DUPFD_CLOEXEC, 10)
        child_output = fcntl.fcntl(child_write, fcntl.F_DUPFD_CLOEXEC, 10)
        os.close(child_read); os.close(child_write)
        wrapper = ("import os,sys; a=int(sys.argv[1]); b=int(sys.argv[2]); "
                   "os.dup2(a,3); os.dup2(b,4); os.close(a); os.close(b); "
                   "os.execv(sys.argv[3],sys.argv[3:])")
        args = [self.binary, "--user-data-dir=" + str(self.profile), "--remote-debugging-pipe",
                "--no-first-run", "--no-default-browser-check", "--new-window", *self.extra_args, "about:blank"]
        environment = dict(os.environ)
        if self.background:
            environment.update(DISPLAY=self.display.name, XAUTHORITY=str(self.display.authority), XDG_SESSION_TYPE="x11")
            environment.pop("WAYLAND_DISPLAY", None)
            args.insert(1, "--ozone-platform=x11")
        if self.sink:
            environment["PULSE_SINK"] = self.sink
        try:
            with self.log.open("ab") as log:
                self.proc = subprocess.Popen([sys.executable, "-c", wrapper, str(child_input), str(child_output), *args],
                    pass_fds=(child_input, child_output), stdin=subprocess.DEVNULL, stdout=log,
                    stderr=log, env=environment, start_new_session=True)
        except BaseException:
            os.close(parent_read); os.close(parent_write)
            raise
        finally:
            os.close(child_input); os.close(child_output)
        self.writer_fd = parent_write
        os.set_blocking(parent_write, False)
        self.reader = asyncio.StreamReader(limit=4*1024*1024)
        self.read_transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(self.reader), os.fdopen(parent_read, "rb", buffering=0))
        self.reader_task = asyncio.create_task(self.receive())
        if self.background:
            async def watch_display():
                while self.display.proc and self.display.proc.poll() is None:
                    await asyncio.sleep(.3)
                self.closed.set()
            self.display_task = asyncio.create_task(watch_display())
        target = await self.call("Target.createTarget", {"url":"https://soundcloud.com/you/likes"})
        attached = await self.call("Target.attachToTarget", {"targetId":target["targetId"], "flatten":True})
        self.session = attached["sessionId"]
        await self.call("Page.enable", page=True)
        targets = await self.call("Target.getTargets")
        for item in targets.get("targetInfos", []):
            if item.get("type")=="page" and item.get("url")=="about:blank":
                await self.call("Target.closeTarget", {"targetId":item["targetId"]})

    async def receive(self):
        try:
            while True:
                raw = await self.reader.readuntil(b"\0")
                packet = json.loads(raw[:-1])
                future = self.pending.pop(packet.get("id"), None)
                if future and not future.done():
                    if "error" in packet:
                        future.set_exception(Failure("BROWSER_PROTOCOL_ERROR"))
                    else:
                        future.set_result(packet.get("result", {}))
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError, ValueError):
            pass
        finally:
            self.closed.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(Failure("BROWSER_CLOSED"))
            self.pending.clear()

    async def call(self, method, params=None, page=False):
        if self.closed.is_set():
            raise Failure("BROWSER_CLOSED")
        self.counter += 1
        key = self.counter
        packet = {"id":key, "method":method, "params":params or {}}
        if page:
            packet["sessionId"] = self.session
        raw = json.dumps(packet).encode() + b"\0"
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            offset = 0
            while offset < len(raw):
                try:
                    offset += os.write(self.writer_fd, raw[offset:])
                except BlockingIOError:
                    ready = asyncio.get_running_loop().create_future()
                    def writable():
                        if not ready.done(): ready.set_result(None)
                    asyncio.get_running_loop().add_writer(self.writer_fd, writable)
                    try:
                        await asyncio.wait_for(ready, 8)
                    finally:
                        asyncio.get_running_loop().remove_writer(self.writer_fd)
            return await asyncio.wait_for(future, 22)
        except (BrokenPipeError, asyncio.TimeoutError):
            raise Failure("BROWSER_TIMEOUT") from None
        finally:
            self.pending.pop(key, None)

    async def page(self, op):
        result = await self.call("Runtime.evaluate", {"expression":PAGE+"("+json.dumps(op, ensure_ascii=False)+")",
            "awaitPromise":True, "returnByValue":True, "userGesture":True}, page=True)
        if "exceptionDetails" in result:
            raise Failure("PAGE_SCRIPT_FAILED")
        value = result.get("result", {}).get("value")
        if not isinstance(value, dict):
            raise Failure("PAGE_NOT_READY")
        return value

    async def navigate(self, url):
        if urlsplit(url).netloc != "soundcloud.com" or not url.startswith("https://soundcloud.com/"):
            raise Failure("UNEXPECTED_ORIGIN")
        result = await self.call("Page.navigate", {"url":url}, page=True)
        if result.get("errorText"):
            raise Failure("SOUNDCLOUD_NAVIGATION_FAILED")
        expected = urlsplit(url).path
        for _ in range(80):
            with contextlib.suppress(Failure):
                state = await self.page({"op":"status"})
                if state.get("ready") and urlsplit(state.get("page", "")).path == expected:
                    return
            await asyncio.sleep(.3)
        raise Failure("SOUNDCLOUD_PAGE_TIMEOUT")

    async def close(self):
        try:
            await self.close_browser()
        finally:
            if self.display_task:
                self.display_task.cancel()
                await asyncio.gather(self.display_task, return_exceptions=True)
                self.display_task = None
            await self.display.close()

    async def close_browser(self):
        if self.proc:
            if self.proc.poll() is None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.call("Browser.close"), 2)
                for _ in range(40):
                    if self.proc.poll() is not None: break
                    await asyncio.sleep(.1)
                if self.proc.poll() is None:
                    with contextlib.suppress(ProcessLookupError): os.killpg(self.proc.pid, signal.SIGTERM)
                    for _ in range(30):
                        if self.proc.poll() is not None: break
                        await asyncio.sleep(.1)
                    if self.proc.poll() is None:
                        with contextlib.suppress(ProcessLookupError): os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=5)
        if self.reader_task:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)
        if self.read_transport:
            self.read_transport.close()
        if self.writer_fd is not None:
            os.close(self.writer_fd)
            self.writer_fd = None


class Controller:
    def __init__(self, browser, sink):
        self.browser, self.sink = browser, sink
        self.lock = asyncio.Lock()
        self.stopping = asyncio.Event()

    async def perform(self, action, value=None):
        value = validate(action, value)
        if action == "stop":
            self.stopping.set()
            return {"ok":True, "stopping":True}
        async with self.lock:
            if action in {"status", "diagnose"}:
                try:
                    result = await self.browser.page({"op":action})
                except Failure as error:
                    if self.browser.closed.is_set(): raise
                    result = {"page_error":str(error), "ready":False}
                return dict(result, ok=True, page_loading=not result.get("ready",False),
                    running=True, browser_connected=True, bluetooth_sink=self.sink,
                    background=self.browser.background,
                    virtual_display=self.browser.display.name if self.browser.background else None)
            if action in {"play", "search", "likes", "resume", "next", "previous"}:
                if self.sink and bluetooth_sink() != self.sink:
                    raise Failure("BT_OUTPUT_CHANGED")
            expected = None
            if action == "play":
                expected = value
                current = None
                with contextlib.suppress(Failure):
                    current = await self.browser.page({"op":"status"})
                current_url = current.get("track_url") if isinstance(current, dict) and current.get("ok") else None
                if isinstance(current_url, str) and current_url.rstrip("/") == value.rstrip("/"):
                    # Keep the current stream, playback position and likes/search
                    # queue. Opening the same URL again would reset all three.
                    operation = "resume"
                else:
                    await self.browser.navigate(value)
                    operation = "play-track"
            elif action in {"search", "likes"}:
                url = "https://soundcloud.com/you/likes" if action == "likes" else "https://soundcloud.com/search/sounds?q=" + quote(value)
                await self.browser.navigate(url)
                operation = "play-first"
            else:
                operation = action
            last = None
            for _ in range(65):
                last = await self.browser.page({"op":operation})
                if last.get("ok"):
                    expected = last.get("selected_url") or expected
                    break
                await asyncio.sleep(.3)
            else:
                raise Failure(last.get("code", "PLAY_CONTROL_UNAVAILABLE") if last else "PLAY_CONTROL_UNAVAILABLE")
            if action in {"next", "previous"}:
                await asyncio.sleep(.8)
                return dict(await self.browser.page({"op":"status"}), action=action)
            desired = action != "pause"
            for _ in range(60):
                state = await self.browser.page({"op":"status"})
                actual = state.get("track_url")
                if state.get("playing") is desired and (not desired or not expected or not actual or actual.rstrip("/")==expected.rstrip("/")):
                    return dict(state, action=action, selected_url=expected)
                await asyncio.sleep(.3)
            raise Failure("PLAYBACK_NOT_CONFIRMED" if desired else "PAUSE_NOT_CONFIRMED")


async def serve(window=False):
    p = locations()
    p["state"].mkdir(parents=True, exist_ok=True, mode=0o700)
    if p["state"].is_symlink(): raise Failure("STOP_SYMLINK")
    sink = bluetooth_sink()
    lock = (p["state"] / "controller.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise Failure("CONTROLLER_ALREADY_RUNNING") from None
    if p["socket"].exists():
        lock.close()
        raise Failure("STOP_EXISTING_SOCKET: проверь владельца и останови прежний контроллер")
    browser = Browser(p["profile"], p["state"] / "browser.log", sink, background=not window)
    server = None
    current = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, current.cancel)
    try:
        await browser.start()
        controller = Controller(browser, sink)
        async def handle(reader, writer):
            try:
                raw = await asyncio.wait_for(reader.readline(), 5)
                if len(raw) > MAX_MESSAGE: raise Failure("REQUEST_TOO_LARGE")
                request = json.loads(raw)
                if not isinstance(request, dict) or set(request)-{"action", "value"}: raise Failure("INVALID_REQUEST")
                result = await controller.perform(request.get("action"), request.get("value"))
            except (Failure, OSError, ValueError, asyncio.TimeoutError) as error:
                result = {"ok":False, "error":str(error) if isinstance(error, Failure) else "REQUEST_FAILED"}
            try:
                writer.write(encoded(result)); await writer.drain()
            finally:
                writer.close(); await writer.wait_closed()
        server = await asyncio.start_unix_server(handle, path=str(p["socket"]), limit=MAX_MESSAGE+1)
        p["socket"].chmod(0o600)
        atomic(p["state"] / "controller.pid", str(os.getpid()).encode())
        with contextlib.suppress(FileNotFoundError): (p["state"] / "last-error.json").unlink()
        waits = [asyncio.create_task(controller.stopping.wait()), asyncio.create_task(browser.closed.wait())]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits: task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
    finally:
        if server:
            server.close(); await server.wait_closed()
        await browser.close()
        if server:
            for name in ("control.sock", "controller.pid"):
                with contextlib.suppress(FileNotFoundError): (p["state"] / name).unlink()
        lock.close()


def request(action, value=None, timeout=100):
    validate(action, value)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(locations()["socket"]))
            client.sendall(encoded({"action":action, "value":value}))
            with client.makefile("rb") as stream:
                raw = stream.readline(MAX_MESSAGE+1)
            if not raw or len(raw)>MAX_MESSAGE: raise Failure("INVALID_CONTROL_REPLY")
            result = json.loads(raw)
            if not isinstance(result, dict): raise Failure("INVALID_CONTROL_REPLY")
            return result
    except (FileNotFoundError, ConnectionRefusedError):
        raise Failure("CONTROLLER_NOT_RUNNING: выполни verity-sc start") from None
    except (OSError, ValueError):
        raise Failure("CONTROLLER_TIMEOUT") from None


def start(window=False):
    p = locations()
    if p["socket"].exists():
        with contextlib.suppress(Failure):
            result=request("status", timeout=25)
            if result.get("running"): return result
    display_dependencies(window)
    p["profile"].mkdir(parents=True, exist_ok=True, mode=0o700)
    if profile_process(p["profile"]): raise Failure("PROFILE_IN_USE: закрой прежнее окно SoundCloud в отдельном Chromium")
    bluetooth_sink()
    p["state"].mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("last-error.json",):
        with contextlib.suppress(FileNotFoundError): (p["state"] / name).unlink()
    with (p["state"] / "controller.log").open("ab") as log:
        child = subprocess.Popen([sys.executable,str(Path(__file__).resolve()),"_serve", *(["--window"] if window else [])],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    for _ in range(120):
        if child.poll() is not None:
            try: error=json.loads((p["state"] / "last-error.json").read_text()).get("error")
            except (OSError, ValueError): error=None
            raise Failure(error or "CHROMIUM_START_FAILED")
        if p["socket"].exists():
            with contextlib.suppress(Failure):
                result=request("status", timeout=25)
                if result.get("running"): return result
        time.sleep(.25)
    child.terminate()
    raise Failure("CONTROLLER_START_TIMEOUT")


def wait_stopped():
    for _ in range(150):
        if not locations()["socket"].exists(): return
        time.sleep(.1)
    raise Failure("CONTROLLER_STILL_STOPPING")


def switch_mode(window):
    p = locations()
    if p["socket"].exists():
        current = request("status", timeout=25)
        if current.get("ok") and current.get("running") and current.get("background") is (not window):
            return current
    # Check the destination mode before stopping a working browser.
    display_dependencies(window)
    if p["socket"].exists():
        result = request("stop", timeout=12)
        if not result.get("ok"): raise Failure("STOP_FAILED")
        wait_stopped()
    return start(window=window)


def main():
    if os.geteuid()==0: raise Failure("Запускай без sudo")
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--window", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("action", nargs="?", choices=sorted(ROUTES|{"start","window","background","_serve"}))
    parser.add_argument("value", nargs="?")
    args=parser.parse_args()
    if args.check:
        if args.action or args.window or args.value is not None: raise Failure("CHECK_ARGUMENTS")
        display_dependencies()
        if not (shutil.which("chromium-browser") or shutil.which("chromium")): raise Failure("CHROMIUM_NOT_FOUND")
        if not locations()["profile"].is_dir(): raise Failure("SOUNDCLOUD_PROFILE_MISSING")
        bluetooth_sink()
        print("BACKGROUND_TOOLS_OK"); return 0
    if args.window and args.action != "_serve": raise Failure("USE_WINDOW_COMMAND")
    if args.action in {"start", "_serve", "window", "background"} and args.value is not None:
        raise Failure("UNEXPECTED_ARGUMENT")
    if args.action=="_serve":
        try: asyncio.run(serve(window=args.window))
        except asyncio.CancelledError: pass
        return 0
    if args.action=="start": result=start()
    elif args.action in {"window","background"}: result=switch_mode(window=args.action=="window")
    elif args.action: result=request(args.action,args.value)
    else: parser.print_help(); return 0
    if args.action=="stop" and result.get("ok"):
        wait_stopped()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__=="__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (Failure, OSError, subprocess.SubprocessError) as error:
        message=str(error) if isinstance(error,Failure) else "LOCAL_OPERATION_FAILED"
        if "_serve" in sys.argv:
            with contextlib.suppress(Exception):
                atomic(locations()["state"] / "last-error.json", encoded({"error":message}))
        print(message, file=sys.stderr)
        raise SystemExit(1)
