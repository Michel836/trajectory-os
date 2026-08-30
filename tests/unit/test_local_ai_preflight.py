from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path("scripts/check-local-ai")


def write_fake_ollama(
    tmp_path: Path,
    *,
    num_ctx: int = 65536,
    draft_num_predict: int = 1,
    exists: bool = True,
) -> Path:
    path = tmp_path / "fake-ollama"

    if exists:
        body = f"""#!/usr/bin/env bash
if [[ "$1" == "show" && "$2" == "qwen3.8-dev3090" ]]; then
cat <<'OUT'
  Parameters
    draft_num_predict    {draft_num_predict}
    num_ctx              {num_ctx}
OUT
exit 0
fi
exit 2
"""
    else:
        body = """#!/usr/bin/env bash
if [[ "$1" == "show" ]]; then
    echo 'model not found' >&2
    exit 1
fi
exit 2
"""

    path.write_text(body)
    path.chmod(0o755)
    return path


def valid_pi_model() -> dict:
    return {
        "id": "qwen3.8-dev3090",
        "reasoning": True,
        "input": ["text", "image"],
        "contextWindow": 65536,
        "maxTokens": 32768,
        "compat": {
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": True,
        },
        "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
        },
    }


def write_pi_config(
    tmp_path: Path,
    model: dict | None = None,
) -> Path:
    path = tmp_path / "models.json"

    models = [] if model is None else [model]

    path.write_text(
        json.dumps(
            {
                "providers": {
                    "ollama": {
                        "models": models,
                    }
                }
            }
        )
    )
    return path


def run_preflight(
    fake_ollama: Path,
    pi_config: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["TRAJECTORY_OLLAMA_BIN"] = str(fake_ollama)
    env["TRAJECTORY_PI_MODELS_JSON"] = str(pi_config)

    return subprocess.run(
        [str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_preflight_accepts_valid_configuration(tmp_path: Path) -> None:
    ollama = write_fake_ollama(tmp_path)
    pi_config = write_pi_config(tmp_path, valid_pi_model())

    result = run_preflight(ollama, pi_config)

    assert result.returncode == 0
    assert "READY — local Pi/Qwen3.8 configuration is valid" in result.stdout


def test_preflight_rejects_missing_ollama_alias(tmp_path: Path) -> None:
    ollama = write_fake_ollama(tmp_path, exists=False)
    pi_config = write_pi_config(tmp_path, valid_pi_model())

    result = run_preflight(ollama, pi_config)

    assert result.returncode == 1
    assert "FAIL  Ollama alias qwen3.8-dev3090" in result.stdout


def test_preflight_rejects_wrong_draft_depth(tmp_path: Path) -> None:
    ollama = write_fake_ollama(tmp_path, draft_num_predict=4)
    pi_config = write_pi_config(tmp_path, valid_pi_model())

    result = run_preflight(ollama, pi_config)

    assert result.returncode == 1
    assert "FAIL  Ollama draft_num_predict" in result.stdout


def test_preflight_rejects_wrong_ollama_context(tmp_path: Path) -> None:
    ollama = write_fake_ollama(tmp_path, num_ctx=32768)
    pi_config = write_pi_config(tmp_path, valid_pi_model())

    result = run_preflight(ollama, pi_config)

    assert result.returncode == 1
    assert "FAIL  Ollama num_ctx" in result.stdout


def test_preflight_rejects_missing_pi_entry(tmp_path: Path) -> None:
    ollama = write_fake_ollama(tmp_path)
    pi_config = write_pi_config(tmp_path, None)

    result = run_preflight(ollama, pi_config)

    assert result.returncode == 1
    assert "FAIL  Pi model qwen3.8-dev3090" in result.stdout


def test_preflight_rejects_wrong_pi_limits(tmp_path: Path) -> None:
    ollama = write_fake_ollama(tmp_path)
    model = valid_pi_model()
    model["contextWindow"] = 32768
    model["maxTokens"] = 8192
    pi_config = write_pi_config(tmp_path, model)

    result = run_preflight(ollama, pi_config)

    assert result.returncode == 1
    assert "FAIL  Pi contextWindow" in result.stdout
    assert "FAIL  Pi maxTokens" in result.stdout


def test_preflight_rejects_wrong_pi_capabilities(tmp_path: Path) -> None:
    ollama = write_fake_ollama(tmp_path)
    model = valid_pi_model()
    model["input"] = ["text"]
    model["compat"]["supportsReasoningEffort"] = False
    pi_config = write_pi_config(tmp_path, model)

    result = run_preflight(ollama, pi_config)

    assert result.returncode == 1
    assert "FAIL  Pi input capabilities" in result.stdout
    assert "FAIL  Pi supportsReasoningEffort" in result.stdout


def test_preflight_rejects_malformed_pi_json(tmp_path: Path) -> None:
    ollama = write_fake_ollama(tmp_path)
    pi_config = tmp_path / "models.json"
    pi_config.write_text("{not-json")

    result = run_preflight(ollama, pi_config)

    assert result.returncode == 1
    assert "FAIL  Pi model registry" in result.stdout


def test_preflight_rejects_missing_pi_config(tmp_path: Path) -> None:
    ollama = write_fake_ollama(tmp_path)
    pi_config = tmp_path / "missing-models.json"

    result = run_preflight(ollama, pi_config)

    assert result.returncode == 1
    assert "FAIL  Pi model registry" in result.stdout
