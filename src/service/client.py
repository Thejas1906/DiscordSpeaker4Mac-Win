"""Controller-side handle on the engine process (used by the desktop UI and by tests)."""
from __future__ import annotations

import itertools
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("DiscordSpeaker.client")


def engine_command() -> List[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--engine"]
    return [sys.executable, "-m", "service.engine"]


class EngineError(Exception):
    pass


class EngineClient:
    def __init__(self, on_event: Optional[Callable[[Dict[str, Any]], None]] = None, command: Optional[List[str]] = None,
                 cwd: Optional[str] = None, env: Optional[dict] = None):
        self.on_event = on_event or (lambda e: None)
        self._cmd = command or engine_command()
        self._cwd = cwd or (None if getattr(sys, "frozen", False) else str(Path(__file__).resolve().parents[1]))
        self._env = env
        self._proc: Optional[subprocess.Popen] = None
        self._ids = itertools.count(1)
        self._pending: Dict[int, dict] = {}
        self._lock = threading.Lock()
        self._ready = threading.Event()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    def start(self, timeout: float = 20.0) -> None:
        if self.alive:
            return
        env = dict(os.environ if self._env is None else self._env)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        self._ready.clear()
        self._proc = subprocess.Popen(self._cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      cwd=self._cwd, env=env, text=True, bufsize=1, encoding="utf-8")
        threading.Thread(target=self._read, args=(self._proc,), name="engine-reader", daemon=True).start()
        if not self._ready.wait(timeout):
            self.stop(force=True)
            raise EngineError("engine did not start in time")

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg:
                with self._lock:
                    slot = self._pending.pop(msg["id"], None)
                if slot:
                    slot["msg"] = msg
                    slot["ev"].set()
            else:
                if msg.get("event") == "ready":
                    self._ready.set()
                try:
                    self.on_event(msg)
                except Exception:
                    log.exception("event handler failed")
        # process ended: fail anything pending
        with self._lock:
            for slot in self._pending.values():
                slot["msg"] = {"ok": False, "error": "engine exited"}
                slot["ev"].set()
            self._pending.clear()
        self.on_event({"event": "engine_exit", "code": proc.poll()})

    def request(self, cmd: str, timeout: float = 15.0, **args) -> Any:
        if not self.alive:
            raise EngineError("engine is not running")
        rid = next(self._ids)
        slot = {"ev": threading.Event(), "msg": None}
        with self._lock:
            self._pending[rid] = slot
        try:
            self._proc.stdin.write(json.dumps({"id": rid, "cmd": cmd, **args}) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise EngineError("engine pipe closed")
        if not slot["ev"].wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise EngineError(f"'{cmd}' timed out")
        msg = slot["msg"]
        if not msg.get("ok"):
            raise EngineError(msg.get("error", "failed"))
        return msg.get("result")

    def stop(self, timeout: float = 10.0, force: bool = False) -> None:
        p = self._proc
        if not p:
            return
        if p.poll() is None and not force:
            try:
                self.request("shutdown", timeout=3)
            except EngineError:
                pass
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                p.wait(timeout)
            except subprocess.TimeoutExpired:
                pass
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(3)
        self._proc = None
