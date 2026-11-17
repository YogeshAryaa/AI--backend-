import unittest

from app.local_image import sampling_settings


class LocalImageSettingsTests(unittest.TestCase):
    def test_turbo_uses_fast_sampling(self):
        self.assertEqual(sampling_settings("stabilityai/sd-turbo", 2, 0.0), (2, 0.0))

    def test_classic_compvis_uses_classic_sampling_defaults(self):
        self.assertEqual(sampling_settings("CompVis/stable-diffusion-v1-4", 2, 0.0), (30, 7.5))

    def test_classic_sampling_respects_configured_values(self):
        self.assertEqual(sampling_settings("CompVis/stable-diffusion-v1-4", 35, 8.0), (35, 8.0))

    def test_sampling_is_bounded(self):
        self.assertEqual(sampling_settings("classic-model", 999, 99), (80, 20.0))


if __name__ == "__main__":
    unittest.main()
