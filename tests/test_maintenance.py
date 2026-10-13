import hashlib
import json

from fsm_panel.maintenance import main


def test_cli_preview_hash_checked_apply_and_repeat_noop(tmp_path, capsys):
    template, steam = tmp_path / "panel.txt", tmp_path / "steam.txt"
    template.write_text('"VendorID" "1"\n"DeviceID" "2"\n')
    steam.write_text('"VendorID" "5"\n"DeviceID" "6"\n')
    before = template.read_bytes()
    arguments = ["video", "--template", str(template), "--steam-config", str(steam)]
    assert main(arguments) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["changed"] and preview["operation"] == "preview"
    assert template.read_bytes() == before
    assert main(arguments + ["--apply", "--expect-template-sha256", "wrong"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "video_preview_stale"
    assert template.read_bytes() == before
    assert (
        main(
            arguments + ["--apply", "--expect-template-sha256", hashlib.sha256(before).hexdigest()]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["backup_path"]
    assert main(arguments + ["--apply"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["changed"] is False and result["backup_path"] is None
