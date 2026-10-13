import json
import os
import subprocess

import pytest

from fsm_panel.launch_preview import main


@pytest.fixture
def proposal(tmp_path):
    source, output = tmp_path / "input.json", tmp_path / "proposal.json"
    document = {
        "settings": {
            "steam_path": "C:\\synthetic\\steam.exe",
            "steam_launch_options": "-synthetic",
            "additional_launch_options": "-synthetic_secret",
            "cs_width": 730,
            "cs_height": 480,
            "no_avast_mode": True,
            "steam_launch_only": False,
            "ancient_map_preload": True,
            "running_2vs2": True,
            "user_profile_new_link": "C:\\synthetic-profiles",
            "enable_shaders_caching": False,
        },
        "login": "synthetic",
        "update_mode": True,
        "environment": {"USERPROFILE": "C:\\synthetic-user", "SECRET_TOKEN": "never_export"},
        "facts": {
            "launch_dota2_exists": True,
            "shader_link_exists": False,
            "shader_link_is_symlink": False,
        },
        "ipc_random": 42,
    }
    source.write_text(json.dumps(document), encoding="utf-8")
    return source, output, document


def command(source, output):
    return ["--input", str(source), "--output", str(output)]


def test_proposal_never_executes_or_changes_host_and_redacts_console(proposal, capsys, monkeypatch):
    source, output, _ = proposal

    def forbidden(*args, **kwargs):
        pytest.fail("offline proposal performed an external action")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "makedirs", forbidden)
    before = source.read_bytes()
    assert main(command(source, output)) == 0
    console = capsys.readouterr().out
    assert "synthetic" not in console and "never_export" not in console
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["executable"] is False
    assert "570" in payload["argv"]  # Original broad replacement also changes width.
    assert "+map" in payload["argv"] and "de_inferno" in payload["argv"]
    assert payload["environment_overrides"]["VPROJECT"] == "1"
    assert "SECRET_TOKEN" not in output.read_text(encoding="utf-8")
    assert "never_export" not in output.read_text(encoding="utf-8")
    assert source.read_bytes() == before


def test_existing_output_or_input_is_not_overwritten(proposal, capsys):
    source, output, _ = proposal
    output.write_text("preserve")
    assert main(command(source, output)) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "launch_output_exists"
    assert output.read_text() == "preserve"
    before = source.read_bytes()
    assert main(command(source, source)) == 2
    assert source.read_bytes() == before


@pytest.mark.parametrize("content", ["{", "[]", '{"settings":{}}', '{"settings":"secret"}'])
def test_invalid_inputs_leave_no_output(proposal, capsys, content):
    source, output, _ = proposal
    source.write_text(content)
    assert main(command(source, output)) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "launch_invalid_input"
    assert not output.exists()


def test_normal_mode_does_not_export_inherited_environment(proposal):
    source, output, document = proposal
    document["settings"]["no_avast_mode"] = False
    source.write_text(json.dumps(document))
    assert main(command(source, output)) == 0
    payload = json.loads(output.read_text())
    assert payload["environment_mode"] == "inherit"
    assert payload["environment_overrides"] is None
