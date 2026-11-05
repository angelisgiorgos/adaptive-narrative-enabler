import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ui_authoring as authoring


class AuthoringTests(unittest.TestCase):
    def test_settings_round_trip_and_invalid_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(authoring, "SETTINGS_PATH", Path(directory) / "ui.yml"):
                settings = authoring.load_app_settings()
                settings["default_candidates"] = 4
                authoring.save_app_settings(settings)
                self.assertEqual(authoring.load_app_settings(), settings)
                for key, value in (("server_port", 70000), ("max_web_sessions", 0),
                                   ("crossover_probability", float("nan")),
                                   ("default_candidates", 100), ("queue_max_size", True)):
                    with self.subTest(key=key), self.assertRaises(ValueError):
                        authoring.save_app_settings({**settings, key: value})
                self.assertEqual(authoring.load_app_settings(), settings)

if __name__ == "__main__":
    unittest.main()
