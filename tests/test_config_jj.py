import sys
import tempfile
from pathlib import Path
from unittest import TestCase


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import config_jj


class ConfigJjTests(TestCase):
    def test_render_escapes_toml_strings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = config_jj.render(Path(tmp), name='Ada "Countess"', email=r"ada\dev@example.com")

            self.assertEqual(
                out.read_text(encoding="utf-8"),
                '[user]\nname = "Ada \\"Countess\\""\nemail = "ada\\\\dev@example.com"\n',
            )
