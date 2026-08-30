from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path("scripts/setup-local-ai")
PREFLIGHT = Path("scripts/check-local-ai")


def write_fake_ollama(tmp_path: Path) -> tuple[Path, Path]:
    script = tmp_path / "fake-ollama"
    state = tmp_path / "ollama-repaired"

    script.write_text(
        """#!/usr/bin/env bash
set -u

STATE="${FAKE_OLLAMA_STATE:?missing FAKE_OLLAMA_STATE}"

if [[ "$1" == "show" && "$2" == "qwen3.8:27b" ]]; then
    echo "Model qwen3.8:27b"
    exit 0
fi

if [[ "$1" == "show" && "$2" == "qwen3.8-dev3090" ]]; then
    if [[ -f "$STATE" ]]; then
        cat <<'OUT'
  Parameters
    draft_num_predict    1
    num_ctx              65536
OUT
        exit 0
    fi

    cat <<'OUT'
  Parameters
    draft_num_predict    4
    num_ctx              32768
OUT
    exit 0
fi

if [[ "$1" == "create" && "$2" == "qwen3.8-dev3090" ]]; then
    touch "$STATE"
    echo "success"
    exit 0
fi

echo "unexpected invocation: $*" >&2
exit 2
"""
    )
    script.chmod(0o755)
    return script, state


def valid_pi_model() -> dict:
    return {
        "id": "qwen3.8-dev3090",
        "name": "Ollama - Qwen 3.8 27B TrajectoryOS 64K RTX3090 MTP",
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


def write_pi_registry(
    tmp_path: Path,
    *,
    include_target: bool,
    malformed_target: bool = False,
) -> Path:
    models = [
        {
            "id": "qwen3.8-dev64",
            "name": "Ollama - Qwen 3.8 27B TrajectoryOS 64K",
        },
        {
            "id": "unrelated-model",
            "name": "Must survive repair",
        },
    ]

    if include_target:
        model = valid_pi_model()

        if malformed_target:
            model["contextWindow"] = 32768
            model["maxTokens"] = 8192

        models.append(model)

    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "ollama": {
                        "baseUrl": "http://localhost:11434/v1",
                        "api": "openai-completions",
                        "apiKey": "ollama",
                        "models": models,
                    }
                }
            },
            indent=2,
        )
        + "\n"
    )
    return path


def run_setup(
    ollama: Path,
    state: Path,
    registry: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["TRAJECTORY_OLLAMA_BIN"] = str(ollama)
    env["TRAJECTORY_PI_MODELS_JSON"] = str(registry)
    env["FAKE_OLLAMA_STATE"] = str(state)

    return subprocess.run(
        [str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def run_preflight(
    ollama: Path,
    state: Path,
    registry: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["TRAJECTORY_OLLAMA_BIN"] = str(ollama)
    env["TRAJECTORY_PI_MODELS_JSON"] = str(registry)
    env["FAKE_OLLAMA_STATE"] = str(state)

    return subprocess.run(
        [str(PREFLIGHT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_setup_refuses_without_apply(tmp_path: Path) -> None:
    ollama, state = write_fake_ollama(tmp_path)
    registry = write_pi_registry(tmp_path, include_target=False)

    before = registry.read_text()

    result = run_setup(ollama, state, registry)

    assert result.returncode == 2
    assert "REFUSED — no changes made." in result.stdout
    assert registry.read_text() == before
    assert not state.exists()
    assert not list(tmp_path.glob("models.json.bak-v04-*"))


def test_setup_repairs_alias_and_missing_pi_entry(tmp_path: Path) -> None:
    ollama, state = write_fake_ollama(tmp_path)
    registry = write_pi_registry(tmp_path, include_target=False)

    result = run_setup(ollama, state, registry, "--apply")

    assert result.returncode == 0
    assert "FIX   Rebuilding Ollama alias qwen3.8-dev3090" in result.stdout
    assert "PASS  Pi registry entry qwen3.8-dev3090 repaired" in result.stdout
    assert "REPAIRED — local AI configuration updated" in result.stdout

    assert state.exists()

    data = json.loads(registry.read_text())
    models = data["providers"]["ollama"]["models"]

    target = next(
        model for model in models if model.get("id") == "qwen3.8-dev3090"
    )

    assert target == valid_pi_model()

    unrelated = next(
        model for model in models if model.get("id") == "unrelated-model"
    )
    assert unrelated["name"] == "Must survive repair"

    backups = list(tmp_path.glob("models.json.bak-v04-*"))
    assert len(backups) == 1


def test_setup_repairs_existing_invalid_pi_entry(tmp_path: Path) -> None:
    ollama, state = write_fake_ollama(tmp_path)
    registry = write_pi_registry(
        tmp_path,
        include_target=True,
        malformed_target=True,
    )

    result = run_setup(ollama, state, registry, "--apply")

    assert result.returncode == 0

    data = json.loads(registry.read_text())
    models = data["providers"]["ollama"]["models"]

    target = next(
        model for model in models if model.get("id") == "qwen3.8-dev3090"
    )

    assert target == valid_pi_model()
    assert len(list(tmp_path.glob("models.json.bak-v04-*"))) == 1


def test_setup_is_idempotent_after_repair(tmp_path: Path) -> None:
    ollama, state = write_fake_ollama(tmp_path)
    registry = write_pi_registry(tmp_path, include_target=False)

    first = run_setup(ollama, state, registry, "--apply")

    assert first.returncode == 0

    first_contents = registry.read_text()
    first_backups = list(tmp_path.glob("models.json.bak-v04-*"))

    second = run_setup(ollama, state, registry, "--apply")

    assert second.returncode == 0
    assert "NO CHANGES NEEDED" in second.stdout
    assert registry.read_text() == first_contents
    assert len(list(tmp_path.glob("models.json.bak-v04-*"))) == len(
        first_backups
    )


def test_repaired_configuration_passes_preflight(tmp_path: Path) -> None:
    ollama, state = write_fake_ollama(tmp_path)
    registry = write_pi_registry(tmp_path, include_target=False)

    setup = run_setup(ollama, state, registry, "--apply")
    assert setup.returncode == 0

    preflight = run_preflight(ollama, state, registry)

    assert preflight.returncode == 0
    assert "READY — local Pi/Qwen3.8 configuration is valid" in preflight.stdout


def test_setup_fails_for_missing_pi_registry(tmp_path: Path) -> None:
    ollama, state = write_fake_ollama(tmp_path)
    registry = tmp_path / "missing.json"

    result = run_setup(ollama, state, registry, "--apply")

    assert result.returncode == 1
    assert "Pi registry does not exist" in result.stdout


def test_setup_fails_for_malformed_pi_registry(tmp_path: Path) -> None:
    ollama, state = write_fake_ollama(tmp_path)
    registry = tmp_path / "models.json"
    registry.write_text("{bad-json")

    result = run_setup(ollama, state, registry, "--apply")

    assert result.returncode == 1
    assert "Cannot parse Pi registry" in result.stdout


def test_setup_preserves_unknown_fields_in_target_entry(
    tmp_path: Path,
) -> None:
    ollama, state = write_fake_ollama(tmp_path)
    registry = write_pi_registry(
        tmp_path,
        include_target=True,
        malformed_target=True,
    )

    data = json.loads(registry.read_text())
    target = next(
        model
        for model in data["providers"]["ollama"]["models"]
        if model.get("id") == "qwen3.8-dev3090"
    )
    target["futureField"] = {"must": "survive"}
    target["compat"]["futureCapability"] = "preserve-me"

    registry.write_text(json.dumps(data, indent=2) + "\n")

    result = run_setup(ollama, state, registry, "--apply")

    assert result.returncode == 0

    repaired = json.loads(registry.read_text())
    repaired_target = next(
        model
        for model in repaired["providers"]["ollama"]["models"]
        if model.get("id") == "qwen3.8-dev3090"
    )

    assert repaired_target["contextWindow"] == 65536
    assert repaired_target["maxTokens"] == 32768
    assert repaired_target["futureField"] == {"must": "survive"}
    assert (
        repaired_target["compat"]["futureCapability"]
        == "preserve-me"
    )


def test_setup_does_not_mutate_ollama_when_pi_registry_is_invalid(
    tmp_path: Path,
) -> None:
    ollama, state = write_fake_ollama(tmp_path)
    registry = tmp_path / "models.json"
    registry.write_text("{bad-json")

    result = run_setup(ollama, state, registry, "--apply")

    assert result.returncode == 1
    assert "Cannot parse Pi registry" in result.stdout
    assert not state.exists()
