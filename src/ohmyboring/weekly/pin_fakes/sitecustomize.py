"""고정 시험 전용 — PYTHONPATH 로 얹으면 자식 파이썬마다 시계·엔진·슬랙·사건 장부가 가짜로 바뀐다.

PIN_FAKES 에 JSON 한 덩이로 시나리오를 준다. 이 모듈은 시험이 아닌 곳에서 불리지 않는다.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

_RAW = os.environ.get("PIN_FAKES")


def _chain_shadowed() -> None:
    """이름이 같은 인터프리터 쪽 sitecustomize(예: Homebrew 의 사이트 경로 추가)를 가리지 않는다."""
    here = os.path.dirname(os.path.abspath(__file__))
    for entry in sys.path:
        candidate = os.path.join(entry or ".", "sitecustomize.py")
        if os.path.abspath(os.path.dirname(candidate)) == here or not os.path.isfile(candidate):
            continue
        spec = importlib.util.spec_from_file_location("_shadowed_sitecustomize", candidate)
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
        return


def _freeze(now_iso: str) -> None:
    real = datetime.datetime
    fixed = real.fromisoformat(now_iso)

    class Frozen(real):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    datetime.datetime = Frozen


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body


def _fake_urlopen(engine: dict, out_dir: str):
    def urlopen(req, timeout=None):
        with open(os.path.join(out_dir, "engine-call.json"), "w", encoding="utf-8") as handle:
            json.dump({"url": getattr(req, "full_url", req), "timeout": timeout}, handle)
        mode = engine["mode"]
        if mode == "urlerror":
            raise urllib.error.URLError("boom")
        if mode == "hang":
            time.sleep(30)
        if mode == "notjson":
            return _Response(b"<html>502</html>")
        if mode == "list":
            return _Response(b"[1]")
        return _Response(json.dumps(engine.get("reply", {}), ensure_ascii=False).encode("utf-8"))

    return urlopen


def _patch_slack(post: dict, out_dir: str) -> None:
    import slack_sdk.web

    class FakeWebClient:
        def __init__(self, token=None):
            self.token = token

        def chat_postMessage(self, **kwargs):
            with open(os.path.join(out_dir, "posted.json"), "w", encoding="utf-8") as handle:
                json.dump({"token": self.token, **kwargs}, handle, ensure_ascii=False, sort_keys=True)
            if post["mode"] == "refuse":
                raise RuntimeError("channel_not_found\n  detail")
            return {"ts": "1234.5678"}

    slack_sdk.web.WebClient = FakeWebClient


def _patch_events(events: dict, out_dir: str) -> None:
    from ohmyboring.adapters import events as event_log

    def recent(*_args, **_kwargs):
        return events.get("recent", [])

    def append(component, event, status, **fields):
        if events.get("fail"):
            raise OSError("disk full")
        with open(os.path.join(out_dir, "recorded.json"), "w", encoding="utf-8") as handle:
            json.dump([component, event, status, fields], handle, sort_keys=True)

    event_log.recent_events = recent
    event_log.append_event = append


def _clamp_waits(seconds: float) -> None:
    real_run = subprocess.run
    real_join = threading.Thread.join

    def run(*args, **kwargs):
        kwargs["timeout"] = seconds
        return real_run(*args, **kwargs)

    def join(self, timeout=None):
        return real_join(self, seconds if timeout is not None else None)

    subprocess.run = run
    threading.Thread.join = join


def _install() -> None:
    cfg = json.loads(_RAW)
    out_dir = cfg["out_dir"]
    _freeze(cfg["now"])
    urllib.request.urlopen = _fake_urlopen(cfg["engine"], out_dir)
    if "post" in cfg:
        _patch_slack(cfg["post"], out_dir)
    if "events" in cfg:
        _patch_events(cfg["events"], out_dir)
    if "clamp" in cfg:
        _clamp_waits(cfg["clamp"])


_chain_shadowed()
if _RAW:
    _install()
