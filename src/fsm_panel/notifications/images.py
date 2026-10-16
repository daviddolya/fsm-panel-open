"""Bounded offline reconstruction of the original two-item Pillow composition.

No downloader, notification sender, EXIF transpose, or alpha compositing is used.
The limits, format policy and exclusive output are reconstruction safeguards.
"""

import ctypes
import io
import os
import stat
import threading
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000
MAX_OUTPUT_BYTES = 40 * 1024 * 1024
_MODES = {"1", "L", "LA", "P", "RGB", "RGBA", "CMYK"}


class ItemCompositeError(ValueError):
    """Stable diagnostic without filenames, image metadata or decoder messages."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ItemComposite:
    png: bytes = field(repr=False)
    width: int
    height: int


def _check_cancelled(cancelled: threading.Event | None) -> None:
    if cancelled is not None and cancelled.is_set():
        raise ItemCompositeError("image_cancelled")


def _decode(raw: bytes) -> Image.Image:
    if type(raw) is not bytes or not raw:
        raise ItemCompositeError("image_invalid_input")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ItemCompositeError("image_too_large")
    with warnings.catch_warnings(action="error", category=Image.DecompressionBombWarning):
        with Image.open(io.BytesIO(raw), formats=("PNG", "JPEG")) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ItemCompositeError("image_too_many_pixels")
            if getattr(source, "n_frames", 1) != 1:
                raise ItemCompositeError("image_animation_unsupported")
            if source.mode not in _MODES:
                raise ItemCompositeError("image_mode_unsupported")
            source.load()
            return source.copy()


def compose_item_images(
    first: bytes, second: bytes, *, cancelled: threading.Event | None = None
) -> ItemComposite:
    """Return an RGBA PNG; resize the second image to the first with PIL defaults."""
    try:
        _check_cancelled(cancelled)
        with _decode(first) as left:
            _check_cancelled(cancelled)
            with _decode(second) as right:
                _check_cancelled(cancelled)
                # Preserve source mode through resize: P and 1 use NEAREST by default.
                with right.resize(left.size) as resized:
                    _check_cancelled(cancelled)
                    with Image.new(
                        "RGBA", (2 * left.width, left.height), (250, 250, 250)
                    ) as canvas:
                        canvas.paste(left, (0, 0))
                        canvas.paste(resized, (left.width, 0))
                        _check_cancelled(cancelled)
                        with io.BytesIO() as target:
                            canvas.save(target, format="PNG")
                            raw = target.getvalue()
                        if len(raw) > MAX_OUTPUT_BYTES:
                            raise ItemCompositeError("image_output_too_large")
                        _check_cancelled(cancelled)
                        return ItemComposite(raw, canvas.width, canvas.height)
    except ItemCompositeError:
        raise
    except Image.DecompressionBombError, Image.DecompressionBombWarning:
        raise ItemCompositeError("image_too_many_pixels") from None
    except Exception:
        raise ItemCompositeError("image_decode_failed") from None


def _absolute(value: str | Path) -> Path:
    text = os.fspath(value)
    if not text or "://" in text or text.startswith(("\\\\", "//")) or "\x00" in text:
        raise ItemCompositeError("image_local_path_required")
    path = Path(os.path.abspath(text))
    if os.name == "nt":
        if ":" in str(path)[2:] or os.path.isreserved(path):
            raise ItemCompositeError("image_local_path_required")
        # UNC/device names are rejected above; mapped remote drives also stay offline.
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(path.anchor)
        if drive_type not in (2, 3, 5, 6):
            raise ItemCompositeError("image_local_path_required")
    return path


def _identity(details: os.stat_result) -> tuple[int, int]:
    return details.st_dev, details.st_ino


def _check_path(path: Path, *, output: bool = False) -> tuple[tuple[int, int], ...]:
    parents = []
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise ItemCompositeError("image_link_rejected")
        try:
            details = item.lstat()
        except FileNotFoundError:
            if output and item == path:
                continue
            raise ItemCompositeError("image_path_unavailable") from None
        if getattr(details, "st_file_attributes", 0) & 0x400:
            raise ItemCompositeError("image_link_rejected")
        if item != path:
            if not stat.S_ISDIR(details.st_mode):
                raise ItemCompositeError("image_path_unavailable")
            parents.append(_identity(details))
            if output and (
                item.name.casefold().startswith("fsm_panel v.") or (item / "Panel.exe").exists()
            ):
                raise ItemCompositeError("image_original_distribution_rejected")
        elif output:
            raise ItemCompositeError("image_output_exists")
        elif not stat.S_ISREG(details.st_mode):
            raise ItemCompositeError("image_regular_file_required")
    return tuple(parents)


def _read(path: Path) -> bytes:
    parents = _check_path(path)
    before = path.stat()
    if before.st_size > MAX_IMAGE_BYTES:
        raise ItemCompositeError("image_too_large")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise ItemCompositeError("image_source_changed")
        raw = handle.read(MAX_IMAGE_BYTES + 1)
        after = os.fstat(handle.fileno())
    if len(raw) > MAX_IMAGE_BYTES:
        raise ItemCompositeError("image_too_large")
    current = path.stat()
    snapshots = {
        (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) for s in (before, opened, after, current)
    }
    if len(snapshots) != 1 or parents != _check_path(path):
        raise ItemCompositeError("image_source_changed")
    return raw


def _write_new(path: Path, raw: bytes) -> None:
    """Finish an admitted write even if cancellation arrives during commit."""
    created = None
    try:
        with path.open("xb") as handle:
            created = _identity(os.fstat(handle.fileno()))
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise ItemCompositeError("image_output_exists") from None
    except Exception:
        if created is not None:
            try:
                if _identity(path.lstat()) == created:
                    path.unlink()
            except OSError:
                raise ItemCompositeError("image_output_cleanup_failed") from None
        raise ItemCompositeError("image_write_failed") from None


def compose_item_image_files(
    first: str | Path,
    second: str | Path,
    output: str | Path,
    *,
    cancelled: threading.Event | None = None,
) -> ItemComposite:
    """Read two local files and create a new PNG; never replace an existing path.

    Cancellation before commit creates no output. Once exclusive creation begins,
    the write finishes and reports success. Callers must join this work on close.
    """
    try:
        _check_cancelled(cancelled)
        first_path, second_path, output_path = map(_absolute, (first, second, output))
        if output_path.suffix.casefold() != ".png":
            raise ItemCompositeError("image_png_output_required")
        parents = _check_path(output_path, output=True)
        left = _read(first_path)
        _check_cancelled(cancelled)
        right = _read(second_path)
        result = compose_item_images(left, right, cancelled=cancelled)
        if parents != _check_path(output_path, output=True):
            raise ItemCompositeError("image_output_parent_changed")
        _check_cancelled(cancelled)
        _write_new(output_path, result.png)
        return result
    except ItemCompositeError:
        raise
    except Exception:
        raise ItemCompositeError("image_file_failed") from None
