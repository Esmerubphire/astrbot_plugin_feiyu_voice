"""Warm TTS daemon: verifies the release once and keeps the networks resident.

Every cold run pays for a full package verification plus a model load. The daemon does that
work once, publishes an endpoint state file, and then answers requests over a loopback socket
so a sentence only costs the sentence. `feiyu_runtime.tts` forwards to it automatically and
falls back to the one-shot path whenever the daemon is absent or stale.

The daemon writes into the `output_dir` named by the caller, exactly like the one-shot path,
so the service-side worker still finds `result.json` and `speech.wav` where it expects them.
"""

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import socketserver
import sys
import threading
import time
import traceback

from feiyu_runtime import cache, tts
from feiyu_runtime.common import cpu_onnx_sessions, load, save

WARM_TEXT = "你好，这是一次预热。"


def log(path, message):
    line = "%s %s" % (time.strftime("%H:%M:%S"), message)
    print(line, flush=True)
    if path:
        try:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except OSError:
            pass


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server
        self.request.settimeout(600)
        stream = self.request.makefile("rwb")
        started = time.perf_counter()
        try:
            raw = stream.readline(1 << 20)
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self.reply(stream, dict(ok=False, code="protocol", error="malformed request"))
        if not secrets.compare_digest(str(payload.get("token", "")), server.token):
            return self.reply(stream, dict(ok=False, code="protocol", error="bad token"))
        if payload.get("profile") not in (None, server.profile_digest):
            return self.reply(stream, dict(ok=False, code="protocol", error="runtime profile changed"))
        request = dict(payload.get("request") or {})
        output_dir = request.get("output_dir")
        if not output_dir:
            return self.reply(stream, dict(ok=False, code="protocol", error="request has no output_dir"))
        with server.lock:  # one GPU, one sentence at a time
            server.last_used = time.time()
            waited = time.perf_counter() - started
            try:
                result = tts.render(server.ctx, request, output_dir)
                reply = dict(ok=True, timing=dict(style=result.get("style"),
                                                  audio_seconds=round(result["seconds"], 3),
                                                  render_seconds=round(result["elapsed_seconds"], 3),
                                                  queue_seconds=round(waited, 3)))
                log(server.log_path, "rendered %s in %.2fs (queued %.2fs)" % (
                    result.get("style"), result["elapsed_seconds"], waited))
            except Exception as exc:  # noqa: BLE001 - the daemon must survive one bad sentence
                log(server.log_path, "failed: %r" % (exc,))
                log(server.log_path, traceback.format_exc().rstrip())
                reply = dict(ok=False, code="inference", error=repr(exc))
        self.reply(stream, reply)

    @staticmethod
    def reply(stream, value):
        try:
            stream.write(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            stream.flush()
        except OSError:
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def warm(server, styles):
    for style in styles:
        folder = server.work_root / ("warm-" + style)
        shutil.rmtree(folder, ignore_errors=True)
        try:
            result = tts.render(server.ctx, dict(text=WARM_TEXT, style=style), folder)
            log(server.log_path, "warm %s: %.2fs audio in %.2fs" % (style, result["seconds"],
                                                                   result["elapsed_seconds"]))
        except Exception as exc:  # noqa: BLE001
            log(server.log_path, "warm %s failed: %r" % (style, exc))
        finally:
            shutil.rmtree(folder, ignore_errors=True)


def idle_watch(server, seconds):
    while not server.shutdown_requested:
        time.sleep(min(30.0, seconds))
        if server.shutdown_requested:
            return
        if time.time() - server.last_used > seconds:
            log(server.log_path, "idle for %.0fs, shutting down" % seconds)
            server.shutdown_requested = True
            server.shutdown()
            return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9881)
    parser.add_argument("--state-file", type=Path, default=tts.daemon_file())
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument("--work-root", type=Path, default=None)
    parser.add_argument("--warm", default="", help="comma separated styles warmed once at startup")
    parser.add_argument("--idle-timeout", type=float, default=0.0, help="seconds; 0 keeps the model resident")
    args = parser.parse_args()
    profile = load(args.profile)
    state_path = args.state_file.expanduser().resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    work_root = (args.work_root or state_path.parent / "warm-output").expanduser().resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    log_path = (args.log or state_path.parent / "tts-daemon.log").expanduser().resolve()

    import onnxruntime as ort

    sessions = []
    confinement = cpu_onnx_sessions(ort, sessions)
    confinement.__enter__()
    try:
        started = time.perf_counter()
        log(log_path, "starting: package=%s verify=%s" % (args.package, cache.describe()))
        ctx = tts.build(args.package, profile, sessions)
        ctx["daemon"] = True
        log(log_path, "model ready in %.1fs (load %.1fs)" % (time.perf_counter() - started,
                                                            ctx["load_seconds"]))
        server = Server((args.host, args.port), Handler)
        server.ctx, server.log_path, server.work_root = ctx, log_path, work_root
        server.lock = threading.Lock()
        server.token = secrets.token_hex(16)
        server.profile_digest = tts.canonical(profile)
        server.last_used = time.time()
        server.shutdown_requested = False
        save(state_path, dict(host=args.host, port=server.server_address[1], token=server.token,
                              package=str(ctx["package"]), profile=server.profile_digest,
                              pid=os.getpid(), started_at=time.time(), load_seconds=ctx["load_seconds"],
                              torch=ctx["info"]["torch"], gpu=ctx["info"]["gpu"]))
        log(log_path, "listening on %s:%d (state %s)" % (args.host, server.server_address[1], state_path))
        if args.warm:
            warm(server, [part.strip() for part in args.warm.split(",") if part.strip()])
        if args.idle_timeout > 0:
            threading.Thread(target=idle_watch, args=(server, args.idle_timeout), daemon=True).start()

        def stop(signum, frame):
            server.shutdown_requested = True
            log(log_path, "signal %s, stopping" % signum)
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            server.server_close()
            try:
                state_path.unlink()
            except OSError:
                pass
            log(log_path, "stopped")
    finally:
        confinement.__exit__(None, None, None)


if __name__ == "__main__":
    main()
