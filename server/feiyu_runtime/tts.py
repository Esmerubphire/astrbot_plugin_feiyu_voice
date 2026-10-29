"""CosyVoice3 adapter preserving release weights and explicit CPU ONNX sessions.

The adapter can run in two shapes:

* one-shot (`python -m feiyu_runtime.worker --kind tts ...`), the upstream contract: verify,
  load the model, synthesise once, exit;
* forwarded to a warm daemon (`feiyu_runtime.serve`), which keeps the verified package and
  the loaded networks in memory so a request only pays for the sentence itself. A missing,
  stale or mismatched daemon silently falls back to the one-shot path.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time

from feiyu_runtime import cache
from feiyu_runtime.common import (apply_backend_env, checked_path, cpu_onnx_sessions, device_type, load,
                                 prepare, save, sha, verify_audio_library)

DAEMON_FILE_ENV = "FEIYU_TTS_DAEMON_FILE"
MODE_ENV = "FEIYU_TTS_MODE"


def daemon_file():
    explicit = os.environ.get(DAEMON_FILE_ENV)
    if explicit:
        return Path(explicit)
    return Path.home() / ".feiyu" / "tts-daemon.json"


def canonical(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def move_to_xpu(torch, voice, device):
    device = torch.device(device)
    for name in ("llm", "flow", "hift"):
        getattr(voice.model, name).to(device).eval()
    voice.model.device = device
    voice.frontend.device = device
    if sys.platform == "linux":
        # Linux XPU cannot run the float64 weight_norm kernel used by this predictor.
        f0 = voice.model.hift.f0_predictor
        f0.to("cpu").eval()
        original_forward = f0.forward

        def f0_forward(speech_feat, *args, **kwargs):
            return original_forward(speech_feat.cpu(), *args, **kwargs)

        f0.forward = f0_forward


def build(package, profile, sessions):
    """Verify assets and load the model once; the caller owns the ONNX CPU confinement."""
    package = Path(package).resolve()
    manifest = cache.verify_package(package)
    if manifest["schema"] != "feiyu-voice-local-release-v1":
        raise ValueError("Unsupported TTS package")
    repo = cache.verify_repo(profile)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="4")
    apply_backend_env(profile)
    os.chdir(repo)
    sys.path[:0] = [str(repo), str(repo / "third_party/Matcha-TTS")]
    import torch
    from cosyvoice.cli.cosyvoice import AutoModel

    info = prepare(torch, profile)
    verify_audio_library(profile)
    started = time.perf_counter()
    voice = AutoModel(model_dir=str(checked_path(package, manifest["base_model_dir"])),
                      fp16=False, load_trt=False, load_vllm=False)
    if info["backend"] == "xpu":
        # Upstream only knows cuda/CPU: it loads on CPU, then the networks and the
        # device used for its input tensors are moved to the Intel GPU.
        move_to_xpu(torch, voice, info["device"])
    # Verify every main network's actual device.
    for name in ("llm", "flow", "hift"):
        actual = next(getattr(voice.model, name).parameters()).device.type
        if actual != device_type(info["backend"]):
            raise RuntimeError("Unexpected TTS network device: " + name)
    voice.model.llm.eval()
    return dict(package=package, repo=repo, manifest=manifest, torch=torch, voice=voice, info=info,
                sessions=sessions, records=load(package / "files_sha256.json"), style=None,
                load_seconds=time.perf_counter() - started, verified_at=time.time())


def _check_asset(package, records, path):
    record = records.get(Path(path).relative_to(package).as_posix())
    if record and "bytes" in record and Path(path).stat().st_size != record["bytes"]:
        raise ValueError("Selected TTS asset changed on disk: " + str(path))


def render(ctx, request, output_dir):
    """Synthesise one sentence into `output_dir`; the weights are already resident."""
    import numpy as np
    import soundfile as sf

    torch, voice, manifest, package = ctx["torch"], ctx["voice"], ctx["manifest"], ctx["package"]
    selected = manifest["styles"][request.get("style", manifest["default_style"])]
    out = Path(output_dir).resolve()
    if out.is_relative_to(package) or out == ctx["repo"] or ctx["repo"].is_relative_to(out):
        raise ValueError("Output must be outside the package and runtime source")
    out.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    checkpoint = checked_path(package, selected["llm_checkpoint"])
    reference = checked_path(package, selected["reference_audio"])
    _check_asset(package, ctx["records"], checkpoint)
    _check_asset(package, ctx["records"], reference)
    base_llm = checked_path(package, manifest["base_model_dir"]) / "llm.pt"
    if ctx.get("style") != selected["llm_checkpoint"]:
        if ctx.get("style") is not None or checkpoint != base_llm:
            state = torch.load(checkpoint, map_location="cpu", weights_only=True)
            voice.model.llm.load_state_dict(state, strict=True)
            del state
        ctx["style"] = selected["llm_checkpoint"]
    params = manifest["inference"]
    from cosyvoice.utils.common import set_all_random_seed

    set_all_random_seed(params["seed"])
    save(out / "job.json", dict(checkpoint_sha256=selected["llm_sha256"],
                               reference_sha256=selected["reference_sha256"],
                               manifest_sha256=sha(package / "manifest.json"), inference=params, runtime=ctx["info"]))
    with torch.inference_mode():
        chunks = [x["tts_speech"].detach().cpu().numpy()[0] for x in voice.inference_zero_shot(
            request["text"], params["prompt_prefix"] + selected["reference_text"], str(reference),
            stream=False, speed=params["speed"], text_frontend=True)]
    if not chunks:
        raise ValueError("TTS returned no audio")
    audio = np.concatenate(chunks)
    if len(audio) <= 2400 or not np.isfinite(audio).all() or abs(audio).max() <= 1e-4:
        raise ValueError("Invalid TTS output")
    sf.write(out / "native.wav", audio, voice.sample_rate, subtype="FLOAT")
    gain = min(1., .98 / float(abs(audio).max()))
    sf.write(out / "speech.wav", audio * gain, voice.sample_rate, subtype="PCM_16")
    result = dict(status="success", playback_sha256=sha(out / "speech.wav"),
                  output_sha256=sha(out / "native.wav"), sample_rate=voice.sample_rate,
                  seconds=len(audio) / voice.sample_rate, playback_gain=gain, runtime=ctx["info"],
                  onnx_sessions=ctx["sessions"], actual_text_frontend=voice.frontend.text_frontend,
                  checkpoint_sha256=selected["llm_sha256"],
                  elapsed_seconds=time.perf_counter() - started, runtime_rvc=False,
                  style=request.get("style", manifest["default_style"]),
                  warm=bool(ctx.get("daemon")), load_seconds=ctx.get("load_seconds"))
    save(out / "result.json", result)
    return result


def forward(package, profile, request):
    """Send the request to a warm daemon. Returns True when it produced the result."""
    if os.environ.get(MODE_ENV, "").strip().lower() in ("cold", "off", "local"):
        return False
    try:
        state = json.loads(daemon_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    try:
        if Path(state["package"]).resolve() != Path(package).resolve():
            return False
        if state.get("profile") != canonical(profile):
            return False
        payload = json.dumps(dict(request=request, token=state["token"],
                                  package=str(Path(package).resolve()),
                                  profile=state["profile"]), ensure_ascii=False).encode("utf-8") + b"\n"
        with socket.create_connection((state["host"], state["port"]), timeout=10) as sock:
            sock.settimeout(1800)
            sock.sendall(payload)
            buffer = bytearray()
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
    except (OSError, KeyError, ValueError) as exc:
        print("warm daemon unavailable (%s); falling back to a cold run" % exc, file=sys.stderr, flush=True)
        return False
    try:
        reply = json.loads(buffer.decode("utf-8"))
    except ValueError:
        print("warm daemon sent an unreadable reply; falling back", file=sys.stderr, flush=True)
        return False
    if not reply.get("ok"):
        if reply.get("code") == "inference":
            raise RuntimeError("Warm TTS daemon failed: " + str(reply.get("error", "unknown error")))
        print("warm daemon rejected the request (%s); falling back to a cold run"
              % reply.get("error", "unknown reason"), file=sys.stderr, flush=True)
        return False
    print(json.dumps(reply.get("timing", {}), ensure_ascii=False, sort_keys=True), flush=True)
    return True


def main(package, profile, argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--no-daemon", action="store_true")
    args = parser.parse_args(argv)
    request = load(args.request)
    if not args.no_daemon and forward(package, profile, request):
        return 0
    import onnxruntime as ort

    sessions = []
    with cpu_onnx_sessions(ort, sessions):
        ctx = build(package, profile, sessions)
        render(ctx, request, request["output_dir"])
    return 0
