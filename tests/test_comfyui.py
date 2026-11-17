import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ADMIN_TOKEN", "t" * 32)
os.environ.setdefault("APP_SECRET_KEY", "s" * 32)

import httpx  # noqa: E402

from app import comfyui  # noqa: E402
from app.config import cfg  # noqa: E402


class ComfyUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "video.json"
        self.path.write_text(json.dumps({
            "workflow": {
                "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}},
                "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512}},
                "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
                "10": {"class_type": "VideoNode", "inputs": {"length": 24}},
            },
            "inputs": {
                "prompt": "6.inputs.text", "width": "5.inputs.width", "height": "5.inputs.height",
                "seed": "3.inputs.seed", "frames": "10.inputs.length",
            },
            "output_node": "10",
        }), encoding="utf-8")
        self.old_path = cfg.comfyui_video_workflow
        self.old_image_path = cfg.comfyui_image_workflow
        self.old_url = cfg.comfyui_url
        self.old_timeout = cfg.comfyui_timeout_seconds
        cfg.comfyui_video_workflow = str(self.path)
        cfg.comfyui_image_workflow = str(self.path)
        cfg.comfyui_url = "http://comfy.test"
        cfg.comfyui_timeout_seconds = 3

    def tearDown(self):
        cfg.comfyui_video_workflow = self.old_path
        cfg.comfyui_image_workflow = self.old_image_path
        cfg.comfyui_url = self.old_url
        cfg.comfyui_timeout_seconds = self.old_timeout
        self.temp.cleanup()

    def test_prepare_workflow_maps_inputs_without_mutating_template(self):
        spec = comfyui._read_spec("video")
        result = comfyui.prepare_workflow(spec, prompt="A kite flies", width=512, height=288, frames=48)
        self.assertEqual(result["6"]["inputs"]["text"], "A kite flies")
        self.assertEqual(result["5"]["inputs"], {"width": 512, "height": 288})
        self.assertEqual(result["10"]["inputs"]["length"], 48)
        self.assertGreaterEqual(result["3"]["inputs"]["seed"], 0)
        self.assertEqual(spec["workflow"]["6"]["inputs"]["text"], "old")

    def test_bad_input_mapping_is_reported(self):
        with self.assertRaisesRegex(comfyui.ComfyUIError, "does not exist"):
            comfyui.prepare_workflow(
                {"workflow": {"1": {"inputs": {"text": "x"}}}, "inputs": {"prompt": "1.inputs.missing"}},
                prompt="test", width=10, height=10,
            )

    def test_video_workflow_requires_frame_mapping(self):
        spec = json.loads(self.path.read_text(encoding="utf-8"))
        spec["inputs"].pop("frames")
        self.path.write_text(json.dumps(spec), encoding="utf-8")
        self.assertFalse(comfyui.configured("video"))
        with self.assertRaisesRegex(comfyui.ComfyUIError, "frames"):
            comfyui._read_spec("video")

    async def test_video_generation_submits_polls_and_downloads_output(self):
        calls = []

        def handler(request):
            calls.append(request)
            if request.url.path == "/prompt":
                body = json.loads(request.content)
                self.assertEqual(body["prompt"]["6"]["inputs"]["text"], "A kite flies")
                self.assertEqual(body["prompt"]["10"]["inputs"]["length"], 48)
                return httpx.Response(200, json={"prompt_id": "job-1"})
            if request.url.path == "/history/job-1":
                return httpx.Response(200, json={"job-1": {"outputs": {
                    "10": {"videos": [{"filename": "clip.mp4", "subfolder": "", "type": "output"}]},
                }}})
            if request.url.path == "/view":
                return httpx.Response(200, content=b"video-data", headers={"content-type": "video/mp4"})
            return httpx.Response(404)

        real_client = httpx.AsyncClient

        def client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        with patch.object(comfyui.httpx, "AsyncClient", client), patch.object(comfyui.asyncio, "sleep", return_value=None):
            data, mime, filename = await comfyui.generate("video", "A kite flies", 512, 288, frames=48)

        self.assertEqual((data, mime, filename), (b"video-data", "video/mp4", "clip.mp4"))
        self.assertEqual([request.url.path for request in calls], ["/prompt", "/history/job-1", "/view"])


if __name__ == "__main__":
    unittest.main()
