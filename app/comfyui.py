"""ComfyUI API integration for locally hosted image and video workflows."""
import asyncio
import copy
import json
import logging
import random
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import cfg

log = logging.getLogger("rag.comfyui")


class ComfyUIError(RuntimeError):
    pass


def _workflow_path(kind: str) -> Path:
    value = cfg.comfyui_image_workflow if kind == "image" else cfg.comfyui_video_workflow
    if not value:
        raise ComfyUIError(f"COMFYUI_{kind.upper()}_WORKFLOW is not set")
    return Path(value).expanduser()


def configured(kind: str) -> bool:
    try:
        _read_spec(kind)
        return True
    except (OSError, ValueError, ComfyUIError):
        return False


def _read_spec(kind: str) -> dict:
    path = _workflow_path(kind)
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ComfyUIError(f"Could not read the {kind} workflow file: {e}") from e
    except json.JSONDecodeError as e:
        raise ComfyUIError(f"The {kind} workflow file is not valid JSON") from e
    if not isinstance(spec, dict) or not isinstance(spec.get("workflow"), dict):
        raise ComfyUIError(f"The {kind} workflow must contain a 'workflow' object")
    mappings = spec.get("inputs")
    required = {"prompt", "width", "height"} | ({"frames"} if kind == "video" else set())
    if not isinstance(mappings, dict) or not required.issubset(mappings):
        names = ", ".join(sorted(required))
        raise ComfyUIError(f"The {kind} workflow must map these inputs: {names}")
    if set(mappings) - {"prompt", "width", "height", "seed", "frames"}:
        raise ComfyUIError("Workflow input names must be prompt, width, height, seed, or frames")
    check = copy.deepcopy(spec["workflow"])
    for name, path in mappings.items():
        _set_input(check, path, None)
    return spec


def _set_input(workflow: dict, path: str, value) -> None:
    if not isinstance(path, str):
        raise ComfyUIError("Workflow input mappings must be strings")
    try:
        node_id, input_name = path.split(".inputs.", 1)
        node = workflow[node_id]
        inputs = node["inputs"]
    except (ValueError, KeyError, TypeError) as e:
        raise ComfyUIError(f"Invalid workflow input mapping '{path}'") from e
    if not isinstance(inputs, dict) or input_name not in inputs:
        raise ComfyUIError(f"Workflow input '{path}' does not exist")
    inputs[input_name] = value


def prepare_workflow(spec: dict, *, prompt: str, width: int, height: int, frames: int | None = None) -> dict:
    workflow = copy.deepcopy(spec["workflow"])
    values = {"prompt": prompt, "width": width, "height": height, "seed": random.randrange(2**31)}
    if frames is not None:
        values["frames"] = frames
    for name, value in values.items():
        path = spec["inputs"].get(name)
        if path:
            _set_input(workflow, path, value)
    return workflow


def _output_file(outputs: dict, kind: str, output_node: str | None) -> dict:
    nodes = [outputs.get(output_node)] if output_node else list(outputs.values())
    allowed = (".mp4", ".webm", ".mov", ".mkv") if kind == "video" else (".png", ".jpg", ".jpeg", ".webp")
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in ("videos", "gifs", "images"):
            for item in node.get(key, []):
                filename = item.get("filename") if isinstance(item, dict) else None
                if isinstance(filename, str) and Path(filename).suffix.lower() in allowed:
                    return item
    raise ComfyUIError(f"ComfyUI finished but did not return a {kind} file")


async def generate(kind: str, prompt: str, width: int, height: int, frames: int | None = None) -> tuple[bytes, str, str]:
    """Run a configured ComfyUI API workflow and return (bytes, MIME type, filename)."""
    spec = _read_spec(kind)
    workflow = prepare_workflow(spec, prompt=prompt, width=width, height=height, frames=frames)
    base = cfg.comfyui_url.rstrip("/")
    timeout = httpx.Timeout(30.0, connect=5.0)
    deadline = time.monotonic() + cfg.comfyui_timeout_seconds
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(f"{base}/prompt", json={"prompt": workflow})
            response.raise_for_status()
            try:
                prompt_id = response.json().get("prompt_id")
            except ValueError as e:
                raise ComfyUIError("ComfyUI returned invalid JSON when queuing the workflow") from e
            if not prompt_id:
                raise ComfyUIError("ComfyUI did not return a prompt ID")
            while time.monotonic() < deadline:
                await asyncio.sleep(1)
                response = await client.get(f"{base}/history/{quote(prompt_id, safe='')}")
                response.raise_for_status()
                try:
                    history = response.json().get(prompt_id, {})
                except ValueError as e:
                    raise ComfyUIError("ComfyUI returned invalid JSON while checking workflow status") from e
                outputs = history.get("outputs") or {}
                if outputs:
                    output_node = spec.get("output_node")
                    item = _output_file(outputs, kind, str(output_node) if output_node is not None else None)
                    response = await client.get(f"{base}/view", params={
                        "filename": item["filename"],
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                    })
                    response.raise_for_status()
                    mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if kind == "video" and not mime.startswith("video/"):
                        suffix = Path(item["filename"]).suffix.lower()
                        mime = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
                                ".mkv": "video/x-matroska"}.get(suffix, "")
                    if (kind == "video" and not mime.startswith("video/")) or (
                            kind == "image" and not mime.startswith("image/")):
                        raise ComfyUIError(f"ComfyUI returned an unsupported {kind} content type")
                    return response.content, mime, item["filename"]
            raise ComfyUIError(f"ComfyUI {kind} generation timed out after {cfg.comfyui_timeout_seconds} seconds")
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:300]
            raise ComfyUIError(f"ComfyUI returned HTTP {e.response.status_code}: {detail}") from e
        except httpx.HTTPError as e:
            raise ComfyUIError(f"Could not reach ComfyUI: {type(e).__name__}") from e
