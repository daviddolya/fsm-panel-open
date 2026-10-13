"""Independent pure-launch and proposal-export checks; no command is executed."""

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import pytest

from fsm_panel import launch_preview
from fsm_panel.adapters.launch_plan import (
    LaunchFacts,
    LaunchSettings,
    build_launch_plan,
    prepare_loginusers,
)


@pytest.fixture
def inputs():
    settings = LaunchSettings(
        steam_path=r"C:\synthetic\steam.exe",
        steam_launch_options="",
        additional_launch_options="",
        cs_width=640,
        cs_height=480,
        no_avast_mode=False,
        steam_launch_only=False,
        ancient_map_preload=False,
        running_2vs2=False,
        user_profile_new_link=r"C:\synthetic profiles",
        enable_shaders_caching=True,
    )
    return settings, LaunchFacts(False, False, False)


@pytest.mark.parametrize(
    "options,update,no_avast,expected",
    [
        ("\t  applaunch730 \v\f", False, False, ("\t", "", "730")),
        ("\u200b\u2003", False, False, ("\u200b",)),
        ('"applaunch a"  \n', False, False, ('"', 'a"')),
        ("\t\r\n", True, False, ("-login", "", "-master_ipc_name_override", "test2")),
        ("-applaunch730", False, True, ("-730", "-master_ipc_name_override", "test2")),
        (
            "\u2002applaunch\tX ",
            True,
            True,
            ("-login", "\u2002\tX", "-master_ipc_name_override", "test2"),
        ),
    ],
)
def test_steam_only_order_and_literal_ascii_split(inputs, options, update, no_avast, expected):
    settings, facts = inputs
    settings = replace(
        settings,
        steam_launch_options=options,
        steam_launch_only=True,
        no_avast_mode=no_avast,
        additional_launch_options="must-not-appear",
        ancient_map_preload=True,
    )
    inherited = {"VPROJECT": "unchanged", "synthetic_private": "not-an-argument"}
    plan = build_launch_plan(
        settings,
        login="test",
        update_mode=update,
        environment=inherited,
        facts=replace(facts, launch_dota2_exists=True),
        ipc_random=2,
        steam_path="",
    )
    assert plan.argv == ("", *expected)
    assert inherited["VPROJECT"] == "unchanged"
    assert (plan.environment is None) is (not no_avast)
    assert plan.shader_cache is None


def test_dota_transformation_stops_before_login_filename_and_extra_options(inputs):
    settings, facts = inputs
    plan = build_launch_plan(
        replace(
            settings,
            steam_launch_options="730 -dxlevel 90\u2003",
            cs_width="730x730",
            cs_height="0730",
            no_avast_mode=True,
            ancient_map_preload=True,
            running_2vs2=True,
            additional_launch_options="730 -nojoystick +map de_inferno_extra",
        ),
        login="login730",
        update_mode=True,
        environment={},
        facts=replace(facts, launch_dota2_exists=True),
        ipc_random=730,
    )
    assert plan.main == (
        "-login 570 -master_ipc_name_override login570570"
        " -applaunch 570 -language english +exec autoexec.cfg -w 570x570 -h 0570"
        " -console -condebug -conclearlog -allowmultiple -con_logfile login730.log"
        " 730 -nojoystick +map de_inferno_extra -novid"
    )
    assert "+map" in plan.argv and "de_inferno" not in plan.argv
    assert "-nojoy" not in plan.argv


@pytest.mark.parametrize(
    "now,expected", [(-1.5, -182), (-0.5, -180), (180.5, 0), (181.5, 2), (182.5, 2), (183.5, 4)]
)
def test_login_timestamp_ties_are_rounded_after_offset(now, expected):
    selection = prepare_loginusers(
        r"\\synthetic server\Steam\steam.exe", steam_id=-42, login="synthetic", now=now
    )
    assert selection.timestamp == expected
    assert selection.path == r"\\synthetic server\Steam\config\loginusers.vdf"
    assert f'"Timestamp"        "{expected}"' in selection.content
    assert selection.content.startswith('\n                    "users"\n')
    assert selection.content.endswith("\n                    ")


def input_document(inputs):
    settings, facts = inputs
    return {
        "settings": asdict(settings),
        "facts": asdict(facts),
        "login": "synthetic_user",
        "update_mode": False,
        "environment": {},
    }


def cli_files(tmp_path, document):
    source, output = tmp_path / "input.json", tmp_path / "output.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    arguments = ["--input", str(source), "--output", str(output)]
    return source, output, arguments


def test_export_preserves_initial_directory_exception_policy(inputs, tmp_path):
    document = input_document(inputs)
    document["settings"]["no_avast_mode"] = True
    document["settings"]["enable_shaders_caching"] = False
    document["ipc_random"] = 4
    source, output, _ = cli_files(tmp_path, document)
    summary = launch_preview.preview(source, output)
    assert summary["output_written"]
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["profile_directory"] is not None
    assert result["profile_directory_on_exception"] == "propagate"
    assert result["shader_cache"]["caught_error"] == "missing_userprofile"
    assert result["shader_cache"]["on_exception"] == "report_and_continue"
    assert result["shader_cache"]["operations"] == []


def test_oversized_json_integer_has_content_free_cli_failure(inputs, tmp_path, capsys):
    document = input_document(inputs)
    document["settings"]["cs_width"] = "DIGITS_GO_HERE"
    source, output, arguments = cli_files(tmp_path, document)
    source.write_text(
        source.read_text(encoding="utf-8").replace('"DIGITS_GO_HERE"', "7" * 5000),
        encoding="utf-8",
    )
    before = source.read_bytes()
    previous_limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(4300)
        assert launch_preview.main(arguments) == 2
    finally:
        sys.set_int_max_str_digits(previous_limit)
    assert json.loads(capsys.readouterr().out) == {"error": "launch_invalid_input"}
    assert not output.exists() and source.read_bytes() == before


def test_invalid_unicode_is_detected_before_exclusive_output_creation(inputs, tmp_path, capsys):
    document = input_document(inputs)
    document["login"] = "synthetic\ud800private"
    source, output, arguments = cli_files(tmp_path, document)
    before = source.read_bytes()
    assert launch_preview.main(arguments) == 2
    console = capsys.readouterr()
    assert json.loads(console.out) == {"error": "launch_invalid_unicode"}
    assert "synthetic" not in console.out + console.err
    assert not output.exists() and source.read_bytes() == before


def test_two_exports_cannot_overwrite_the_same_proposal(inputs, tmp_path):
    document = input_document(inputs)
    source, output, _ = cli_files(tmp_path, document)
    barrier = threading.Barrier(2)

    def export():
        barrier.wait(timeout=5)
        try:
            return launch_preview.preview(source, output)
        except FileExistsError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(export) for _ in range(2)]
        results = [job.result(timeout=5) for job in jobs]
    assert sum(isinstance(result, FileExistsError) for result in results) == 1
    assert sum(isinstance(result, dict) and result["output_written"] for result in results) == 1
    proposal = json.loads(output.read_text(encoding="utf-8"))
    assert proposal["kind"] == "offline_launch_proposal" and proposal["executable"] is False
    assert {path.name for path in tmp_path.iterdir()} == {source.name, output.name}


def test_failed_output_write_does_not_print_success_or_caller_data(
    inputs, tmp_path, capsys, monkeypatch
):
    document = input_document(inputs)
    document["settings"]["steam_launch_options"] = "synthetic_private_argument"
    source, output, arguments = cli_files(tmp_path, document)
    original_open = type(output).open

    class FailedOutput:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def write(self, content):
            output.write_bytes(content[:30].encode())
            raise OSError("synthetic private disk diagnostic")

    def output_failure(path, mode="r", *args, **kwargs):
        if path == output and mode == "x":
            return FailedOutput()
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(type(output), "open", output_failure)
    before = source.read_bytes()
    assert launch_preview.main(arguments) == 2
    console = capsys.readouterr()
    assert json.loads(console.out) == {"error": "launch_file_unavailable"}
    assert "synthetic" not in console.out + console.err
    assert source.read_bytes() == before
    assert output.exists()  # A failed write can leave a partial proposal for manual removal.
