"""Independent offline pixel and file-ownership checks for item composition."""

import builtins
import concurrent.futures
import io
import os
import socket
import struct
import subprocess
import threading
import urllib.request
import zlib
from pathlib import Path

import httpx
import pytest
from PIL import Image

from fsm_panel.notifications import images


def _chunk(kind, value):
    return (
        struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    )


def png(width, height, pixels, *, mode="RGBA", palette=b"", transparency=b""):
    """Build tiny source PNGs independently of the production image library."""
    channels, color_type = {"RGBA": (4, 6), "RGB": (3, 2), "L": (1, 0), "P": (1, 3)}[mode]
    values = bytes(component for pixel in pixels for component in pixel)
    assert len(values) == width * height * channels
    rows = b"".join(
        b"\0" + values[row * width * channels : (row + 1) * width * channels]
        for row in range(height)
    )
    result = b"\x89PNG\r\n\x1a\n" + _chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    )
    if palette:
        result += _chunk(b"PLTE", palette)
    if transparency:
        result += _chunk(b"tRNS", transparency)
    return result + _chunk(b"IDAT", zlib.compress(rows)) + _chunk(b"IEND", b"")


FIRST = png(1, 1, [(9, 19, 29, 39)])
SECOND = png(1, 1, [(89, 99, 109, 119)])


def decoded(result):
    assert result.png[:8] == b"\x89PNG\r\n\x1a\n"
    with Image.open(io.BytesIO(result.png)) as output:
        output.load()
        assert output.format == "PNG"
        assert output.mode == "RGBA"
        assert output.size == (result.width, result.height)
        return output.size, list(output.get_flattened_data())


@pytest.fixture
def files(tmp_path):
    first, second, output = (tmp_path / name for name in ("first.png", "second.png", "result.png"))
    first.write_bytes(FIRST)
    second.write_bytes(SECOND)
    return first, second, output


def test_first_geometry_and_unmasked_alpha_determine_both_halves():
    left = [(1, 2, 3, 0), (11, 12, 13, 50), (21, 22, 23, 255)]
    left += [(31, 32, 33, 1), (41, 42, 43, 100), (51, 52, 53, 200)]
    right = (73, 83, 93, 127)
    result = images.compose_item_images(png(3, 2, left), png(1, 1, [right]))
    size, pixels = decoded(result)
    assert size == (6, 2)
    # Default RGBA resize round-trips through premultiplied channels, even for
    # a constant source. Unmasked paste must then retain these RGBA values.
    resized_right = (72, 82, 92, 127)
    assert pixels == left[:3] + [resized_right] * 3 + left[3:] + [resized_right] * 3
    assert "PNG" not in repr(result) and repr(result.png) not in repr(result)


def test_palette_resize_is_nearest_and_retains_per_index_transparency():
    first = png(4, 1, [(20, 30, 40)] * 4, mode="RGB")
    second = png(
        2,
        1,
        [(0,), (1,)],
        mode="P",
        palette=bytes([240, 10, 20, 30, 220, 40]),
        transparency=bytes([0, 64]),
    )
    _, pixels = decoded(images.compose_item_images(first, second))
    assert pixels == [(20, 30, 40, 255)] * 4 + [
        (240, 10, 20, 0),
        (240, 10, 20, 0),
        (30, 220, 40, 64),
        (30, 220, 40, 64),
    ]


def test_rgb_resize_uses_default_bicubic_instead_of_nearest_or_bilinear():
    first = png(4, 1, [(20, 30, 40, 255)] * 4)
    second = png(2, 1, [(255, 0, 0), (0, 0, 255)], mode="RGB")
    _, pixels = decoded(images.compose_item_images(first, second))
    assert pixels[4:] == [
        (255, 0, 0, 255),
        (202, 0, 53, 255),
        (53, 0, 202, 255),
        (0, 0, 255, 255),
    ]


def test_grayscale_source_converts_on_paste_and_keeps_row_order():
    first = png(3, 2, [(4, 5, 6, 0)] * 6)
    second = png(2, 1, [(0,), (255,)], mode="L")
    _, pixels = decoded(images.compose_item_images(first, second))
    expected = [(4, 5, 6, 0)] * 3 + [(0, 0, 0, 255), (128, 128, 128, 255), (255, 255, 255, 255)]
    assert pixels == expected * 2


def test_jpeg_exif_orientation_does_not_rotate_the_first_geometry():
    source = io.BytesIO()
    with Image.new("RGB", (2, 3), (100, 110, 120)) as picture:
        exif = picture.getexif()
        exif[274] = 6
        picture.save(source, format="JPEG", exif=exif)
    result = images.compose_item_images(source.getvalue(), SECOND)
    assert decoded(result)[0] == (4, 3)


def test_bytes_composition_has_no_file_or_network_dependency(monkeypatch):
    Image.init()

    def forbidden(*args, **kwargs):
        raise AssertionError("composition attempted an external operation")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(Path, "open", forbidden)
        patch.setattr(socket, "create_connection", forbidden)
        patch.setattr(socket.socket, "connect", forbidden)
        patch.setattr(urllib.request, "urlopen", forbidden)
        patch.setattr(httpx, "Client", forbidden)
        patch.setattr(httpx, "AsyncClient", forbidden)
        result = images.compose_item_images(FIRST, SECOND)
    assert decoded(result) == ((2, 1), [(9, 19, 29, 39), (89, 99, 109, 119)])


def test_successful_file_composition_owns_only_its_new_destination(files):
    first, second, output = files
    before = [
        (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in files[:2]
    ]
    result = images.compose_item_image_files(first, second, output)
    assert output.read_bytes() == result.png
    assert before == [
        (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in files[:2]
    ]
    assert sorted(path.name for path in output.parent.iterdir()) == [
        "first.png",
        "result.png",
        "second.png",
    ]
    assert decoded(result) == ((2, 1), [(9, 19, 29, 39), (89, 99, 109, 119)])


def test_two_simultaneous_creators_cannot_overwrite_one_another(files):
    first, second, output = files
    start = threading.Barrier(2)

    def create():
        start.wait(timeout=5)
        try:
            return images.compose_item_image_files(first, second, output)
        except Exception as error:
            return error

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]
    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(successes) == len(failures) == 1
    assert isinstance(failures[0], images.ItemCompositeError)
    assert failures[0].code == "image_output_exists"
    assert output.read_bytes() == successes[0].png
    assert decoded(successes[0])[0] == (2, 1)


@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize(
    "raw, code",
    [
        (b"", "image_invalid_input"),
        (b"private malformed image", "image_decode_failed"),
        (FIRST[:45], "image_decode_failed"),
        (bytearray(FIRST), "image_invalid_input"),
        (memoryview(FIRST), "image_invalid_input"),
    ],
)
def test_invalid_bytes_are_rejected_with_content_free_errors(raw, code, position):
    pair = [FIRST, SECOND]
    pair[position] = raw
    with pytest.raises(images.ItemCompositeError) as caught:
        images.compose_item_images(*pair)
    assert str(caught.value) == caught.value.code == code
    assert "private" not in repr(caught.value)


@pytest.mark.parametrize("position", [0, 1])
def test_declared_pixel_limit_rejects_before_loading_pixel_data(position, monkeypatch):
    oversized = b"\x89PNG\r\n\x1a\n" + _chunk(
        b"IHDR", struct.pack(">IIBBBBB", 4_000_001, 1, 8, 6, 0, 0, 0)
    )
    oversized += _chunk(b"IDAT", zlib.compress(b"\0")) + _chunk(b"IEND", b"")
    pair = [FIRST, SECOND]
    pair[position] = oversized
    original_load = Image.Image.load
    oversized_loads = []

    def guarded_load(picture, *args, **kwargs):
        if picture.width * picture.height > 4_000_000:
            oversized_loads.append(picture.size)
            raise AssertionError("pixel allocation preceded the dimension limit")
        return original_load(picture, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", guarded_load)
    with pytest.raises(images.ItemCompositeError, match="^image_too_many_pixels$"):
        images.compose_item_images(*pair)
    assert not oversized_loads


@pytest.mark.parametrize("position", [0, 1])
def test_encoded_size_limit_is_enforced_for_bytes_and_file_sources(files, position):
    oversized = b"x" * (8 * 1024 * 1024 + 1)
    pair = [FIRST, SECOND]
    pair[position] = oversized
    with pytest.raises(images.ItemCompositeError, match="^image_too_large$"):
        images.compose_item_images(*pair)
    with files[position].open("wb") as source:
        source.truncate(len(oversized))
    with pytest.raises(images.ItemCompositeError, match="^image_too_large$"):
        images.compose_item_image_files(*files)
    assert not files[2].exists()
    # Windows would reject this rename if a file handle had escaped the failure.
    renamed = files[position].with_suffix(".closed")
    files[position].rename(renamed)
    assert renamed.stat().st_size == len(oversized)


@pytest.mark.parametrize("format", ["GIF", "PNG"])
def test_multiframe_or_unapproved_format_is_rejected(format):
    target = io.BytesIO()
    with Image.new("RGBA", (1, 1), "red") as first:
        with Image.new("RGBA", (1, 1), "blue") as second:
            first.save(target, format=format, save_all=True, append_images=[second], duration=50)
    expected = "image_animation_unsupported" if format == "PNG" else "image_decode_failed"
    with pytest.raises(images.ItemCompositeError, match=f"^{expected}$"):
        images.compose_item_images(FIRST, target.getvalue())


@pytest.mark.parametrize("kind", ["file", "directory", "first", "second", "hardlink"])
def test_existing_destination_and_source_aliases_are_never_replaced(files, kind):
    first, second, output = files
    if kind == "file":
        output.write_bytes(b"private preexisting output")
    elif kind == "directory":
        output.mkdir()
    elif kind in ("first", "second"):
        output = first if kind == "first" else second
    else:
        os.link(first, output)
    before = {path: path.read_bytes() for path in (first, second, output) if path.is_file()}
    with pytest.raises(images.ItemCompositeError, match="^image_output_exists$"):
        images.compose_item_image_files(first, second, output)
    assert {path: path.read_bytes() for path in before} == before
    if kind == "directory":
        assert output.is_dir() and not list(output.iterdir())
    if kind == "hardlink":
        assert output.samefile(first)


@pytest.mark.parametrize("marker", ["name", "executable"])
def test_original_distribution_guards_apply_to_every_output_ancestor(files, tmp_path, marker):
    first, second, _ = files
    root = tmp_path / ("fSm_PaNeL v.9.synthetic" if marker == "name" else "synthetic-original")
    parent = root / "nested" / "output"
    parent.mkdir(parents=True)
    if marker == "executable":
        (root / "Panel.exe").write_bytes(b"synthetic marker only")
    with pytest.raises(images.ItemCompositeError, match="^image_original_distribution_rejected$"):
        images.compose_item_image_files(first, second, parent / "result.png")
    assert not list(parent.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows junction path protection")
@pytest.mark.parametrize("position", [0, 1, 2])
def test_junction_ancestor_cannot_redirect_input_or_output(files, tmp_path, position):
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "first.png").write_bytes(FIRST)
    (actual / "second.png").write_bytes(SECOND)
    alias = tmp_path / "alias"
    command = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(alias), str(actual)],
        capture_output=True,
        check=False,
    )
    assert command.returncode == 0, "synthetic junction creation failed"
    before = {path.name: path.read_bytes() for path in actual.iterdir()}
    arguments = list(files)
    arguments[position] = alias / files[position].name
    try:
        assert alias.is_junction()
        with pytest.raises(images.ItemCompositeError, match="^image_link_rejected$"):
            images.compose_item_image_files(*arguments)
        assert {path.name: path.read_bytes() for path in actual.iterdir()} == before
        assert not files[2].exists()
    finally:
        alias.rmdir()  # Remove only this synthetic link, never its target tree.


@pytest.mark.parametrize("position", [0, 1, 2])
@pytest.mark.parametrize("unlocal", ["https://private.invalid/icon.png", "//private.invalid/x.png"])
def test_remote_paths_are_rejected_before_any_file_or_network_access(
    files, position, unlocal, monkeypatch
):
    arguments = list(files)
    arguments[position] = unlocal
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("remote paths must be rejected before access")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", forbidden)
        patch.setattr(socket, "create_connection", forbidden)
        patch.setattr(socket.socket, "connect", forbidden)
        with pytest.raises(images.ItemCompositeError, match="^image_local_path_required$"):
            images.compose_item_image_files(*arguments)
    assert not attempted and not files[2].exists()


def test_cancelled_input_is_rejected_before_decode_or_destination_creation(files, monkeypatch):
    cancelled = threading.Event()
    cancelled.set()
    accesses = []

    def forbidden(*args, **kwargs):
        accesses.append(True)
        raise AssertionError("cancelled job opened an input")

    with monkeypatch.context() as patch:
        patch.setattr(Image, "open", forbidden)
        patch.setattr(Path, "open", forbidden)
        with pytest.raises(images.ItemCompositeError, match="^image_cancelled$"):
            images.compose_item_images(FIRST, SECOND, cancelled=cancelled)
        with pytest.raises(images.ItemCompositeError, match="^image_cancelled$"):
            images.compose_item_image_files(*files, cancelled=cancelled)
    assert not accesses and not files[2].exists()


def test_cancellation_after_exclusive_creation_finishes_and_reports_the_png(files, monkeypatch):
    cancelled = threading.Event()
    output = files[2]
    original_open = Path.open
    created = []

    def cancel_after_open(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if path == output and mode == "xb":
            created.append(path)
            cancelled.set()
        return handle

    monkeypatch.setattr(Path, "open", cancel_after_open)
    result = images.compose_item_image_files(*files, cancelled=cancelled)
    assert created == [output] and cancelled.is_set()
    assert output.read_bytes() == result.png
    assert decoded(result)[0] == (2, 1)


@pytest.mark.parametrize("position", [0, 1])
def test_malformed_file_releases_inputs_without_creating_output(files, position):
    files[position].write_bytes(b"private malformed synthetic file")
    with pytest.raises(images.ItemCompositeError, match="^image_decode_failed$"):
        images.compose_item_image_files(*files)
    assert not files[2].exists()
    for path in files[:2]:
        before = path.read_bytes()
        renamed = path.with_suffix(".released")
        path.rename(renamed)
        assert renamed.read_bytes() == before


def test_encoding_failure_does_not_create_a_destination_or_leak_diagnostics(files, monkeypatch):
    attempts = []

    def failed_encode(picture, target, *args, **kwargs):
        attempts.append(True)
        target.write(b"private partial encoding")
        raise OSError("private encoder diagnostic")

    monkeypatch.setattr(Image.Image, "save", failed_encode)
    with pytest.raises(images.ItemCompositeError, match="^image_decode_failed$") as caught:
        images.compose_item_image_files(*files)
    assert attempts == [True] and not files[2].exists()
    assert "private" not in repr(caught.value)
    assert files[0].read_bytes() == FIRST and files[1].read_bytes() == SECOND


@pytest.mark.parametrize("deny_cleanup", [False, True])
def test_failed_durable_write_removes_only_its_output_or_reports_retained_file(
    files, monkeypatch, deny_cleanup
):
    output = files[2]
    fsync_attempts = []

    def fail_sync(descriptor):
        fsync_attempts.append(output.exists())
        raise OSError("private storage diagnostic")

    original_unlink = Path.unlink

    def fail_own_cleanup(path, *args, **kwargs):
        if path == output:
            raise PermissionError("private cleanup diagnostic")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", fail_sync)
    if deny_cleanup:
        monkeypatch.setattr(Path, "unlink", fail_own_cleanup)
    code = "image_output_cleanup_failed" if deny_cleanup else "image_write_failed"
    with pytest.raises(images.ItemCompositeError, match=f"^{code}$") as caught:
        images.compose_item_image_files(*files)
    assert fsync_attempts == [True]
    assert output.exists() is deny_cleanup
    assert "private" not in repr(caught.value)
    assert files[0].read_bytes() == FIRST and files[1].read_bytes() == SECOND
    if deny_cleanup:
        # Cleanup failure retains the file for inspection and closes its handle.
        renamed = output.with_suffix(".retained")
        output.rename(renamed)
        assert renamed.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_replaced_output_parent_is_detected_after_encoding(files, tmp_path, monkeypatch):
    first, second, _ = files
    parent, displaced = tmp_path / "destination", tmp_path / "displaced"
    parent.mkdir()
    (parent / "keep").write_bytes(b"previous parent content")
    original_save = Image.Image.save
    encoded = []

    def replace_parent(picture, *args, **kwargs):
        result = original_save(picture, *args, **kwargs)
        encoded.append(True)
        parent.rename(displaced)
        parent.mkdir()
        return result

    monkeypatch.setattr(Image.Image, "save", replace_parent)
    with pytest.raises(images.ItemCompositeError, match="^image_output_parent_changed$"):
        images.compose_item_image_files(first, second, parent / "out.png")
    assert encoded == [True] and not list(parent.iterdir())
    assert {path.name: path.read_bytes() for path in displaced.iterdir()} == {
        "keep": b"previous parent content"
    }
