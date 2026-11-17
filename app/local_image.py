"""Image AI on this server, no third party: Stable Diffusion Turbo through `diffusers`.

* Runs locally; prompts and generated images stay on this server.
* Default is SD-Turbo for fast generation. Set IMAGE_LOCAL_MODEL=CompVis/stable-diffusion-v1-4
  to use the original CompVis Stable Diffusion v1.4 Diffusers checkpoint instead.
* Model weights are downloaded from Hugging Face on first use; they are not bundled in this app ZIP.
  CompVis v1.4 may require a Hugging Face account/token and accepting the model licence.
* Turbo normally uses 1-4 steps and guidance 0; classic SD normally uses 20-50 steps and guidance ~7.5.
* CPU is supported but can be very slow; a compatible NVIDIA GPU is strongly recommended.
"""
import io
import logging
import random
import threading

from .config import cfg

log = logging.getLogger("rag.local_image")
_pipe = None
_lock = threading.Lock()


def installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("diffusers") is not None


def _load():
    global _pipe
    if _pipe is None:
        try:
            import torch
            from diffusers import AutoPipelineForText2Image
        except ImportError as e:
            raise RuntimeError("local image AI is not installed (pip install diffusers)") from e
        gpu = torch.cuda.is_available()
        dtype = torch.float16 if gpu else torch.float32
        log.info("Loading local image model %s on %s", cfg.image_local_model, "GPU" if gpu else "CPU")
        try:  # the fp16 files are half the download; on CPU they are converted to float32
            pipe = AutoPipelineForText2Image.from_pretrained(cfg.image_local_model, variant="fp16", torch_dtype=dtype)
        except (OSError, ValueError):
            pipe = AutoPipelineForText2Image.from_pretrained(cfg.image_local_model, torch_dtype=dtype)
        pipe = pipe.to("cuda" if gpu else "cpu")
        pipe.set_progress_bar_config(disable=True)
        try:
            pipe.enable_attention_slicing()  # less RAM, same picture
        except Exception:
            pass
        _pipe = pipe
    return _pipe


def warm() -> None:
    with _lock:
        _load()


def sampling_settings(model_id: str, configured_steps: int, configured_guidance: float) -> tuple[int, float]:
    """Resolve safe defaults for distilled Turbo models vs classic Stable Diffusion."""
    turbo = "turbo" in (model_id or "").lower()
    if turbo:
        steps = max(1, min(int(configured_steps or 2), 8))
        guidance = 0.0 if configured_guidance is None else max(0.0, min(float(configured_guidance), 2.0))
    else:
        requested = int(configured_steps or 30)
        # The app's Turbo default is 2; when switching to classic SD, promote that
        # Turbo-only setting to a sensible classic-model default.
        steps = 30 if requested <= 8 else max(10, min(requested, 80))
        guidance = 7.5 if configured_guidance is None or float(configured_guidance) == 0 else max(1.0, min(float(configured_guidance), 20.0))
    return steps, guidance


def _native(width: int, height: int) -> tuple[int, int]:
    """Same shape as asked, at the model's own ~512 px scale (multiples of 64)."""
    base = cfg.image_local_size
    ar = max(0.5, min(2.0, width / max(1, height)))
    if ar >= 1:
        return round(base * ar / 64) * 64, base
    return base, round(base / ar / 64) * 64


def generate(prompt: str, width: int, height: int) -> bytes:
    """PNG bytes. Blocking (call it in a thread); one image at a time."""
    import torch
    from PIL import Image

    w, h = _native(width, height)
    with _lock:
        pipe = _load()
        gen = torch.Generator("cpu").manual_seed(random.randrange(2**31))
        steps, guidance = sampling_settings(cfg.image_local_model, cfg.image_local_steps, cfg.image_local_guidance)
        img = pipe(prompt=prompt, num_inference_steps=steps, guidance_scale=guidance,
                   width=w, height=h, generator=gen).images[0]
    target = max(width, height)
    if max(img.size) < target:
        scale = target / max(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
