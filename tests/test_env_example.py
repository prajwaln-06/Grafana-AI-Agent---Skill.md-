"""`.env.example` is generated from Settings; it must list every setting and match the generator exactly."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _generator():
    spec = importlib.util.spec_from_file_location("generate_env_example", REPO / "scripts" / "generate_env_example.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_env_example_matches_the_settings_exactly():
    gen = _generator()
    committed = (REPO / ".env.example").read_text(encoding="utf-8")
    assert committed == gen.render(), "run: python scripts/generate_env_example.py"


def test_every_setting_appears_and_the_required_key_has_no_value():
    from app.config import Settings
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    for name in Settings.model_fields:
        assert f"{name.upper()}=" in text, name
    assert "GEMINI_API_KEY=   # REQUIRED" in text
    # No secret-looking value is ever written: optional secrets are empty.
    assert "# GRAFANA_SERVICE_ACCOUNT_TOKEN=\n" in text and "# API_KEY=\n" in text


def test_check_mode_reports_up_to_date(capsys):
    assert _generator().main(["--check"]) == 0
