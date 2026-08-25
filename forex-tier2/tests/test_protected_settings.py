import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from config import settings as settings_module


class ProtectedSettingsTests(unittest.TestCase):
    def test_mt5_login_and_password_never_fall_back_to_config_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "mt5": {
                            "login": "must-not-be-used",
                            "password": "must-not-be-used",
                            "server": "Example-Demo",
                        }
                    }
                )
            )
            with patch.object(settings_module, "CONFIG_PATH", config_path), patch.dict(
                os.environ,
                {"MT5_LOGIN": "", "MT5_PASSWORD": "", "MT5_SERVER": ""},
                clear=False,
            ):
                loaded = settings_module._load()

        self.assertEqual(loaded["mt5"]["login"], "")
        self.assertEqual(loaded["mt5"]["password"], "")
        self.assertEqual(loaded["mt5"]["server"], "Example-Demo")


if __name__ == "__main__":
    unittest.main()