import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

from fsm_panel.notifications import images
from fsm_panel.notifications.images import (
    ItemCompositeError,
    compose_item_image_files,
    compose_item_images,
)


def encoded(mode="RGBA", size=(2, 1), color=(7, 13, 29, 255), *, format="PNG", **kwargs):
    with Image.new(mode, size, color) as image, io.BytesIO() as stream:
        image.save(stream, format=format, **kwargs)
        return stream.getvalue()


def pixels(result):
    with Image.open(io.BytesIO(result.png)) as image:
        assert image.mode == "RGBA" and image.format == "PNG"
        assert image.size == (result.width, result.height)
        return list(image.get_flattened_data())


def test_unmasked_paste_preserves_alpha_and_hidden_rgb_instead_of_blending():
    result = compose_item_images(encoded(color=(10, 20, 30, 0)), encoded(color=(70, 80, 90, 128)))
    assert (result.width, result.height) == (4, 1)
    assert pixels(result) == [(10, 20, 30, 0)] * 2 + [(70, 80, 90, 128)] * 2
    assert "PNG" not in repr(result) and "png=" not in repr(result)


def test_mismatched_rgb_uses_default_bicubic_and_stretches_to_first_geometry():
    with Image.new("RGB", (2, 1)) as second, io.BytesIO() as stream:
        second.putdata([(0, 0, 0), (255, 255, 255)])
        second.save(stream, format="PNG")
        result = compose_item_images(encoded(size=(4, 1)), stream.getvalue())
    assert (result.width, result.height) == (8, 1)
    assert pixels(result)[4:] == [
        (0, 0, 0, 255),
        (53, 53, 53, 255),
        (202, 202, 202, 255),
        (255, 255, 255, 255),
    ]


def test_palette_keeps_nearest_resize_and_transparency():
    with Image.new("P", (2, 1)) as second, io.BytesIO() as stream:
        second.putpalette([255, 0, 0, 0, 0, 255] + [0] * 762)
        second.putdata([0, 1])
        second.save(stream, format="PNG", transparency=0)
        result = compose_item_images(encoded(size=(4, 2)), stream.getvalue())
    row = [(255, 0, 0, 0)] * 2 + [(0, 0, 255, 255)] * 2
    values = pixels(result)
    assert values[4:8] == row and values[12:16] == row


@pytest.mark.parametrize(
    "mode,color,expected",
    [
        ("1", 1, (255, 255, 255, 255)),
        ("L", 77, (77, 77, 77, 255)),
        ("LA", (77, 22), (77, 77, 77, 22)),
    ],
)
def test_supported_grayscale_modes(mode, color, expected):
    result = compose_item_images(encoded(mode, color=color), encoded())
    assert pixels(result)[:2] == [expected] * 2


def test_jpeg_exif_orientation_is_not_applied_and_metadata_not_copied():
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private-synthetic-description"
    raw = encoded("RGB", (3, 1), (255, 0, 0), format="JPEG", exif=exif)
    result = compose_item_images(raw, encoded())
    assert (result.width, result.height) == (6, 1)
    with Image.open(io.BytesIO(result.png)) as image:
        assert not image.getexif() and not image.info


@pytest.mark.parametrize("mode,color", [("L", 88), ("CMYK", (0, 20, 30, 0))])
def test_jpeg_grayscale_and_cmyk_are_supported(mode, color):
    result = compose_item_images(encoded(mode, color=color, format="JPEG"), encoded())
    assert len(pixels(result)) == 4


@pytest.mark.parametrize("raw", [b"", b"private-invalid-image", b"GIF89a", None, bytearray(b"x")])
def test_invalid_input_is_sanitized(raw, caplog):
    with pytest.raises(ItemCompositeError) as raised:
        compose_item_images(raw, encoded())
    assert "private-invalid-image" not in str(raised.value) + repr(raised.value) + caplog.text


def test_rejects_unsupported_formats_and_16bit_mode():
    for raw in (encoded(format="GIF"), encoded("I;16", color=1234)):
        with pytest.raises(ItemCompositeError):
            compose_item_images(raw, encoded())


def test_animated_png_is_rejected():
    with Image.new("RGBA", (2, 1), "red") as a, Image.new("RGBA", (2, 1), "blue") as b:
        with io.BytesIO() as stream:
            a.save(stream, format="PNG", save_all=True, append_images=[b])
            with pytest.raises(ItemCompositeError, match="image_animation_unsupported"):
                compose_item_images(stream.getvalue(), encoded())


def test_bounds_checked_before_decode_and_before_allocation(monkeypatch):
    good = encoded()
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", len(good) - 1)
    with pytest.raises(ItemCompositeError, match="image_too_large"):
        compose_item_images(good, good)
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 1024)
    monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 1)
    with pytest.raises(ItemCompositeError, match="image_too_many_pixels"):
        compose_item_images(good, good)


@pytest.fixture
def files(tmp_path):
    left, right, output = (tmp_path / name for name in ("first.png", "second.png", "new.png"))
    left.write_bytes(encoded())
    right.write_bytes(encoded(color=(44, 55, 66, 77)))
    return left, right, output


def test_file_workflow_preserves_inputs_and_creates_exact_result(files):
    left, right, output = files
    before = left.read_bytes(), right.read_bytes()
    result = compose_item_image_files(left, right, output)
    assert output.read_bytes() == result.png
    assert (left.read_bytes(), right.read_bytes()) == before
    with pytest.raises(ItemCompositeError, match="image_output_exists"):
        compose_item_image_files(left, right, output)
    assert output.read_bytes() == result.png


def test_input_cannot_be_output_even_with_hardlink(files):
    left, right, output = files
    os.link(left, output)
    for destination in (left, right, output):
        with pytest.raises(ItemCompositeError, match="image_output_exists"):
            compose_item_image_files(left, right, destination)


def test_concurrent_creators_never_replace_the_winner(files):
    left, right, output = files

    def create():
        try:
            return compose_item_image_files(left, right, output)
        except ItemCompositeError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert sum(result == "image_output_exists" for result in results) == 1
    winner = next(result for result in results if not isinstance(result, str))
    assert output.read_bytes() == winner.png


def test_cancel_before_write_does_not_create_file(files, monkeypatch):
    left, right, output = files
    cancelled = threading.Event()
    original = images.compose_item_images

    def compose(*args, **kwargs):
        result = original(*args, **kwargs)
        cancelled.set()
        return result

    monkeypatch.setattr(images, "compose_item_images", compose)
    with pytest.raises(ItemCompositeError, match="image_cancelled"):
        compose_item_image_files(left, right, output, cancelled=cancelled)
    assert not output.exists()


def test_cancel_after_write_admission_reports_saved_output(files, monkeypatch):
    left, right, output = files
    cancelled = threading.Event()
    original = images._write_new

    def write(path, raw):
        cancelled.set()
        original(path, raw)

    monkeypatch.setattr(images, "_write_new", write)
    result = compose_item_image_files(left, right, output, cancelled=cancelled)
    assert output.read_bytes() == result.png


def test_failed_write_removes_only_created_output(files, monkeypatch):
    left, right, output = files

    def fail(_):
        raise OSError("private-synthetic-path")

    monkeypatch.setattr(images.os, "fsync", fail)
    with pytest.raises(ItemCompositeError, match="image_write_failed"):
        compose_item_image_files(left, right, output)
    assert not output.exists() and left.exists() and right.exists()


def test_original_destination_and_nonlocal_paths_are_rejected(files, tmp_path):
    left, right, _ = files
    original = tmp_path / "original"
    original.mkdir()
    (original / "Panel.exe").write_bytes(b"synthetic-marker")
    for output in (original / "new.png", r"\\invalid\share\new.png", "https://invalid/new.png"):
        with pytest.raises(ItemCompositeError):
            compose_item_image_files(left, right, output)
    assert sorted(p.name for p in original.iterdir()) == ["Panel.exe"]


def test_parent_junction_is_rejected(files, tmp_path):
    left, right, _ = files
    alias = tmp_path / "alias"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(tmp_path), str(alias))
    else:
        alias.symlink_to(tmp_path, target_is_directory=True)
    try:
        with pytest.raises(ItemCompositeError, match="image_link_rejected"):
            compose_item_image_files(left, right, alias / "new.png")
        assert not (tmp_path / "new.png").exists()
    finally:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()
