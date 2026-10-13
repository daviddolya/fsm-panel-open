import codecs
import hashlib
import os
import stat
import subprocess
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from fsm_panel.adapters import video
from fsm_panel.adapters.video import (
    MAX_VIDEO_BYTES,
    VideoApplyError,
    VideoStalePreviewError,
    VideoTemplateError,
    apply_video_template,
    preview_video_template,
)


def config(vendor="10", device="20", *, newline="\n", extra=""):
    return newline.join(
        [
            '"SyntheticVideo"',
            "{",
            f'  "VendorID" "{vendor}"',
            f'  "DeviceID" "{device}"',
            extra,
            "}",
        ]
    )


def files(tmp_path, template=None, source=None):
    template_path = tmp_path / "template.txt"
    source_path = tmp_path / "steam-source.txt"
    template_path.write_bytes((config() if template is None else template).encode())
    source_path.write_bytes((config("30", "40") if source is None else source).encode())
    return template_path, source_path


@pytest.mark.parametrize("policy", ["fields", "legacy"])
@pytest.mark.parametrize(
    "old,new,changed",
    [
        (("10", "20"), ("10", "20"), False),
        (("10", "20"), ("30", "20"), True),
        (("10", "20"), ("10", "40"), True),
        (("10", "20"), ("30", "40"), True),
        (("0010", "0020"), ("10", "20"), True),
    ],
)
def test_or_branch_and_string_ids(tmp_path, policy, old, new, changed):
    template, source = files(tmp_path, config(*old), config(*new))
    preview = preview_video_template(template, source, policy)
    assert preview.changed is changed
    assert (preview.before_vendor_id, preview.before_device_id) == old
    assert (preview.after_vendor_id, preview.after_device_id) == new
    assert template.read_text() == config(*old)
    assert source.read_text() == config(*new)
    assert preview.template_sha256 == hashlib.sha256(template.read_bytes()).hexdigest()
    assert preview.steam_config_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()


def test_safe_default_changes_only_field_values_without_collisions(tmp_path):
    original = config("1", "2", extra='"unrelated" "12" // version 1 and 2')
    template, source = files(tmp_path, original, config("2", "3"))
    preview = preview_video_template(template, source)
    assert preview.policy == "fields"
    assert preview.rendered_content == config("2", "3", extra='"unrelated" "12" // version 1 and 2')
    assert preview.after_vendor_id == "2"


def test_legacy_replacements_are_global_vendor_then_device(tmp_path):
    original = config("1", "2", extra='"unrelated" "12" // version 1 and 2')
    template, source = files(tmp_path, original, config("2", "3"))
    preview = preview_video_template(template, source, "legacy")
    assert preview.rendered_content == config("3", "3", extra='"unrelated" "33" // version 3 and 3')
    assert (preview.source_vendor_id, preview.source_device_id) == ("2", "3")
    assert (preview.after_vendor_id, preview.after_device_id) == ("3", "3")


def test_legacy_runs_both_replacements_when_only_device_differs(tmp_path):
    template, source = files(tmp_path, config("12", "2"), config("12", "3"))
    preview = preview_video_template(template, source, "legacy")
    assert preview.before_vendor_id == preview.source_vendor_id == "12"
    assert preview.after_vendor_id == "13"
    assert preview.after_device_id == "3"


def test_equal_template_ids_follow_legacy_first_replacement(tmp_path):
    template, source = files(tmp_path, config("1", "1"), config("2", "3"))
    preview = preview_video_template(template, source, "legacy")
    assert (preview.after_vendor_id, preview.after_device_id) == ("2", "2")
    assert (preview.source_vendor_id, preview.source_device_id) == ("2", "3")


def test_legacy_uses_first_regex_match_and_supports_unicode_digits(tmp_path):
    original = config("١", "٢", extra='"VendorID" "٤"')
    template, source = files(tmp_path, original, config("٣", "٤"))
    preview = preview_video_template(template, source, "legacy")
    assert preview.rendered_content == config("٣", "٤", extra='"VendorID" "٤"')


@pytest.mark.parametrize("policy", ["fields", "legacy"])
def test_noop_never_writes_or_creates_backup(tmp_path, monkeypatch, policy):
    original = config()
    template, source = files(tmp_path, original, original)
    preview = preview_video_template(template, source, policy)
    original_stat = template.stat()

    def forbidden(*args, **kwargs):
        pytest.fail("unchanged preview attempted a write")

    monkeypatch.setattr(video, "_stage", forbidden)
    monkeypatch.setattr(video.os, "replace", forbidden)
    result = apply_video_template(preview)
    assert not result.changed
    assert result.backup_path is None
    assert template.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert template.stat().st_ino == original_stat.st_ino
    assert sorted(path.name for path in tmp_path.iterdir()) == ["steam-source.txt", "template.txt"]


def test_preview_is_immutable_and_rendered_text_is_not_in_repr(tmp_path):
    template, source = files(tmp_path, config(extra="synthetic_private_text"))
    preview = preview_video_template(template, source)
    with pytest.raises(FrozenInstanceError):
        preview.changed = False
    assert "synthetic_private_text" not in repr(preview)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "encoding,bom",
    [
        ("utf-8", b""),
        ("utf-8", codecs.BOM_UTF8),
        ("utf-16-le", codecs.BOM_UTF16_LE),
        ("utf-16-be", codecs.BOM_UTF16_BE),
    ],
)
def test_apply_preserves_encoding_bom_newlines_and_source(tmp_path, newline, encoding, bom):
    template, source = files(tmp_path)
    original = bom + config(newline=newline, extra="// synthetic café").encode(encoding)
    template.write_bytes(original)
    source_bytes = source.read_bytes()
    source_stat = source.stat()
    preview = preview_video_template(template, source)
    result = apply_video_template(preview)
    assert result.changed
    assert result.template_path == template
    assert result.backup_path is not None
    assert result.backup_path.read_bytes() == original
    assert template.read_bytes() == bom + config(
        "30", "40", newline=newline, extra="// synthetic café"
    ).encode(encoding)
    assert source.read_bytes() == source_bytes
    assert source.stat().st_mtime_ns == source_stat.st_mtime_ns
    assert source.stat().st_ino == source_stat.st_ino
    assert not list(tmp_path.glob("*.tmp"))


def test_repeated_apply_requires_fresh_preview_and_keeps_backups(tmp_path):
    template, source = files(tmp_path)
    first = preview_video_template(template, source)
    result = apply_video_template(first)
    with pytest.raises(VideoStalePreviewError):
        apply_video_template(first)
    source.write_text(config("50", "60"))
    second = apply_video_template(preview_video_template(template, source))
    assert second.backup_path != result.backup_path
    assert result.backup_path.read_text() == config()
    assert second.backup_path.read_text() == config("30", "40")


@pytest.mark.parametrize("which", ["template", "source"])
@pytest.mark.parametrize("mutation", ["bytes", "identity", "deleted"])
def test_stale_source_or_template_is_rejected(tmp_path, which, mutation):
    template, source = files(tmp_path)
    preview = preview_video_template(template, source)
    target = template if which == "template" else source
    if mutation == "bytes":
        target.write_text(config("80", "90"))
    elif mutation == "identity":
        replacement = tmp_path / "replacement.txt"
        replacement.write_bytes(target.read_bytes())
        os.replace(replacement, target)
    else:
        target.unlink()
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(VideoStalePreviewError) as error:
        apply_video_template(preview)
    assert error.value.code == "video_preview_stale"
    assert {path: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_stale_noop_is_rejected(tmp_path):
    template, source = files(tmp_path, config(), config())
    preview = preview_video_template(template, source)
    source.write_text(config("30", "40"))
    with pytest.raises(VideoStalePreviewError):
        apply_video_template(preview)


def test_forged_preview_cannot_write_unreviewed_content(tmp_path):
    template, source = files(tmp_path)
    preview = preview_video_template(template, source)
    forged = replace(preview, rendered_content="synthetic forged content")
    with pytest.raises(VideoStalePreviewError):
        apply_video_template(forged)
    assert template.read_text() == config()


@pytest.mark.parametrize("policy", ["fields", "legacy"])
@pytest.mark.parametrize("which", ["template", "source"])
@pytest.mark.parametrize(
    "invalid",
    [config().replace('"VendorID" "10"', '"VendorID" "-1"'), config().replace("DeviceID", "other")],
)
def test_missing_or_malformed_fields_fail_without_writes(tmp_path, policy, which, invalid):
    template, source = files(tmp_path)
    target = template if which == "template" else source
    target.write_text(invalid)
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(VideoTemplateError, match="video_missing_or_invalid_"):
        preview_video_template(template, source, policy)
    assert {path: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("which", ["template", "source"])
def test_safe_fields_rejects_ambiguous_duplicate_keys(tmp_path, which):
    template, source = files(tmp_path)
    target = template if which == "template" else source
    target.write_text(config(extra='"VendorID" "99"'))
    with pytest.raises(VideoTemplateError, match="video_ambiguous_vendorid"):
        preview_video_template(template, source)


@pytest.mark.parametrize("raw", [b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff", b"\xff", b"\x00"])
def test_unsupported_encoding_is_explicitly_rejected(tmp_path, raw):
    template, source = files(tmp_path)
    template.write_bytes(raw)
    with pytest.raises(VideoTemplateError, match="video_unsupported_encoding"):
        preview_video_template(template, source)


@pytest.mark.parametrize("which", ["template", "source"])
def test_input_size_is_bounded(tmp_path, which):
    template, source = files(tmp_path)
    target = template if which == "template" else source
    target.write_bytes(b"x" * (MAX_VIDEO_BYTES + 1))
    with pytest.raises(VideoTemplateError, match="video_file_too_large"):
        preview_video_template(template, source)


def test_identifier_size_is_bounded(tmp_path):
    template, source = files(tmp_path, config("1" * 129))
    with pytest.raises(VideoTemplateError, match="video_identifier_too_long"):
        preview_video_template(template, source)


def test_legacy_expansion_is_bounded_before_allocating(tmp_path):
    template, source = files(
        tmp_path, config("1", "2", extra="1" * 100_000), config("3" * 128, "4")
    )
    with pytest.raises(VideoTemplateError, match="video_output_too_large"):
        preview_video_template(template, source, "legacy")


def test_unknown_policy_and_missing_file_have_safe_codes(tmp_path):
    template, source = files(tmp_path)
    with pytest.raises(VideoTemplateError, match="video_unknown_policy"):
        preview_video_template(template, source, "unknown")
    source.unlink()
    with pytest.raises(VideoTemplateError) as error:
        preview_video_template(template, source)
    assert error.value.code == "video_file_read_failed"


def test_invalid_path_has_stable_validation_code(tmp_path):
    template, _ = files(tmp_path)
    with pytest.raises(VideoTemplateError) as error:
        preview_video_template(template, "invalid\x00path")
    assert error.value.code in {"video_invalid_path", "video_file_read_failed"}


def test_same_file_and_relative_alias_cannot_be_source(tmp_path, monkeypatch):
    template, _ = files(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(VideoTemplateError, match="video_source_is_template"):
        preview_video_template(template, Path(".") / template.name)


def test_hardlinked_template_is_rejected(tmp_path):
    template, source = files(tmp_path)
    original = tmp_path / "original.txt"
    os.link(template, original)
    with pytest.raises(VideoTemplateError, match="video_template_hardlink_rejected"):
        preview_video_template(template, source)
    assert template.read_bytes() == original.read_bytes() == config().encode()


def make_symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        pytest.skip(
            f"symlink creation unavailable: {error.winerror if os.name == 'nt' else error.errno}"
        )


@pytest.mark.parametrize("which", ["template", "source"])
def test_file_symlinks_are_rejected(tmp_path, which):
    template, source = files(tmp_path)
    link = tmp_path / "link.txt"
    make_symlink(link, template if which == "template" else source)
    with pytest.raises(VideoTemplateError, match="video_link_path_rejected"):
        preview_video_template(
            link if which == "template" else template, link if which == "source" else source
        )


def test_directory_symlink_is_rejected(tmp_path):
    directory = tmp_path / "copies"
    directory.mkdir()
    template, source = files(directory)
    link = tmp_path / "alias"
    make_symlink(link, directory, directory=True)
    with pytest.raises(VideoTemplateError, match="video_link_path_rejected"):
        preview_video_template(link / template.name, source)


def test_link_inserted_after_preview_is_rejected(tmp_path):
    template, source = files(tmp_path)
    preview = preview_video_template(template, source)
    template.unlink()
    make_symlink(template, source)
    with pytest.raises(VideoStalePreviewError):
        apply_video_template(preview)
    assert source.read_text() == config("30", "40")


@pytest.mark.parametrize("which", ["template", "source", "parent"])
def test_symlink_guard_is_enforced_without_os_link_privileges(tmp_path, monkeypatch, which):
    template, source = files(tmp_path)
    rejected = {"template": template, "source": source, "parent": tmp_path}[which]
    original_is_symlink = Path.is_symlink

    def is_symlink(path):
        return path == rejected or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    with pytest.raises(VideoTemplateError, match="video_link_path_rejected"):
        preview_video_template(template, source)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction fixture")
def test_windows_directory_junction_is_rejected(tmp_path):
    directory = tmp_path / "copies"
    directory.mkdir()
    template, source = files(directory)
    junction = tmp_path / "junction"
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(junction), str(directory)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, "synthetic junction creation failed"
    try:
        assert junction.is_junction()
        with pytest.raises(VideoTemplateError, match="video_link_path_rejected"):
            preview_video_template(junction / template.name, source)
    finally:
        # Removing the junction itself never traverses or removes its target.
        junction.rmdir()


def test_hardlink_inserted_after_preview_is_rejected(tmp_path):
    template, source = files(tmp_path)
    preview = preview_video_template(template, source)
    template.unlink()
    os.link(source, template)
    with pytest.raises(VideoStalePreviewError):
        apply_video_template(preview)
    assert source.read_text() == config("30", "40")


def test_readonly_template_rejected_without_changing_permissions(tmp_path):
    template, source = files(tmp_path)
    template.chmod(stat.S_IREAD)
    try:
        with pytest.raises(VideoTemplateError, match="video_template_read_only"):
            preview_video_template(template, source)
        assert not template.stat().st_mode & stat.S_IWUSR
    finally:
        template.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_original_distribution_marker_and_explicit_roots_are_protected(tmp_path):
    original = tmp_path / "original"
    settings = original / "settings"
    settings.mkdir(parents=True)
    template, source = files(settings)
    with pytest.raises(VideoTemplateError, match="video_protected_template"):
        preview_video_template(template, source, protected_roots=(original,))
    (original / "Panel.exe").write_bytes(b"synthetic inert marker")
    with pytest.raises(VideoTemplateError, match="video_original_distribution_rejected"):
        preview_video_template(template, source)
    assert template.read_text() == config()


def test_stale_check_runs_again_after_staging(tmp_path, monkeypatch):
    template, source = files(tmp_path)
    preview = preview_video_template(template, source)
    original_stage = video._stage

    def concurrent_write(*args):
        result = original_stage(*args)
        source.write_text(config("70", "80"))
        return result

    monkeypatch.setattr(video, "_stage", concurrent_write)
    with pytest.raises(VideoStalePreviewError):
        apply_video_template(preview)
    assert template.read_text() == config()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["steam-source.txt", "template.txt"]


@pytest.mark.parametrize("failure_stage", ["output", "backup", "replace", "after_replace"])
def test_io_failure_preserves_or_restores_original(tmp_path, monkeypatch, failure_stage):
    template, source = files(tmp_path)
    original = template.read_bytes()
    source_original = source.read_bytes()
    preview = preview_video_template(template, source)
    original_sync = video.os.fsync
    original_replace = video.os.replace
    calls = 0

    def fail_sync(descriptor):
        nonlocal calls
        calls += 1
        if calls == (1 if failure_stage == "output" else 2):
            raise OSError("synthetic I/O failure")
        return original_sync(descriptor)

    def fail_replace(src, dst):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure_stage == "after_replace":
                original_replace(src, dst)
            raise OSError("synthetic I/O failure")
        return original_replace(src, dst)

    if failure_stage in ("output", "backup"):
        monkeypatch.setattr(video.os, "fsync", fail_sync)
    else:
        monkeypatch.setattr(video.os, "replace", fail_replace)
    with pytest.raises(VideoApplyError) as error:
        apply_video_template(preview)
    assert error.value.code == "video_apply_failed"
    assert error.value.rollback_succeeded
    assert template.read_bytes() == original
    assert source.read_bytes() == source_original
    assert not list(tmp_path.glob("*.tmp"))
    if failure_stage in ("replace", "after_replace"):
        assert error.value.backup_path.read_bytes() == original
    else:
        assert error.value.backup_path is None
        assert len(list(tmp_path.iterdir())) == 2


def test_failed_rollback_retains_usable_backup(tmp_path, monkeypatch):
    template, source = files(tmp_path)
    original = template.read_bytes()
    preview = preview_video_template(template, source)
    real_replace = video.os.replace
    calls = 0

    def failed_io(src, dst):
        nonlocal calls
        calls += 1
        if calls == 1:
            real_replace(src, dst)
        raise OSError("synthetic install/rollback failure")

    monkeypatch.setattr(video.os, "replace", failed_io)
    with pytest.raises(VideoApplyError) as error:
        apply_video_template(preview)
    assert error.value.code == "video_rollback_failed"
    assert not error.value.rollback_succeeded
    assert error.value.backup_path.read_bytes() == original
    assert template.read_bytes() == config("30", "40").encode()


def test_rollback_does_not_overwrite_concurrent_third_value(tmp_path, monkeypatch):
    template, source = files(tmp_path)
    preview = preview_video_template(template, source)
    concurrent = config("80", "90")

    def failed_io(src, dst):
        template.write_text(concurrent)
        raise OSError("synthetic concurrent update")

    monkeypatch.setattr(video.os, "replace", failed_io)
    with pytest.raises(VideoApplyError) as error:
        apply_video_template(preview)
    assert not error.value.rollback_succeeded
    assert template.read_text() == concurrent
    assert error.value.backup_path.read_text() == config()
