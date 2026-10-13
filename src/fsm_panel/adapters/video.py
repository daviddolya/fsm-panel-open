"""Explicit, offline preparation of a writable CS2 video-template copy.

Only ``legacy`` reproduces the recovered string replacement. Path validation,
bounded decoding, preview checks, backups and atomic writes are new safeguards.
"""

import codecs
import hashlib
import os
import re
import stat
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

VideoReplacementPolicy = Literal["fields", "legacy"]
MAX_VIDEO_BYTES = 1024 * 1024
MAX_IDENTIFIER_CHARS = 128
_LOCK = threading.RLock()
_PATTERNS = {name: re.compile(r'"' + name + r'"\s+"(\d+)"') for name in ("VendorID", "DeviceID")}


class VideoTemplateError(ValueError):
    """A safe, stable error code; messages never include config contents."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class VideoStalePreviewError(VideoTemplateError):
    def __init__(self) -> None:
        super().__init__("video_preview_stale")


class VideoApplyError(OSError):
    """Apply failed; a retained backup is available if one was fully written."""

    def __init__(self, backup_path: Path | None, *, rollback_succeeded: bool) -> None:
        self.code = "video_apply_failed" if rollback_succeeded else "video_rollback_failed"
        self.backup_path = backup_path
        self.rollback_succeeded = rollback_succeeded
        super().__init__(self.code)


@dataclass(frozen=True)
class _FileSnapshot:
    device: int
    inode: int
    size: int
    modified_ns: int
    mode: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _FileSnapshot:
        return cls(
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            stat.S_IMODE(value.st_mode),
        )


@dataclass(frozen=True)
class VideoPreview:
    template_path: Path
    steam_config_path: Path
    policy: VideoReplacementPolicy
    before_vendor_id: str
    before_device_id: str
    source_vendor_id: str
    source_device_id: str
    after_vendor_id: str
    after_device_id: str
    changed: bool
    rendered_content: str = field(repr=False)
    template_sha256: str
    steam_config_sha256: str
    encoding: str
    protected_roots: tuple[Path, ...]
    _bom: bytes = field(repr=False)
    _template_bytes: bytes = field(repr=False)
    _template_snapshot: _FileSnapshot = field(repr=False)
    _steam_snapshot: _FileSnapshot = field(repr=False)


@dataclass(frozen=True)
class VideoApplyResult:
    changed: bool
    template_path: Path
    backup_path: Path | None


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(path))


def _check_path(path: Path, *, template: bool, protected_roots: tuple[Path, ...]) -> None:
    # Do not resolve first: that would conceal an explicitly supplied link.
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise VideoTemplateError("video_link_path_rejected")
    if template:
        resolved = path.resolve(strict=True)
        if any(resolved.is_relative_to(root.resolve()) for root in protected_roots):
            raise VideoTemplateError("video_protected_template")
        if any((parent / "Panel.exe").exists() for parent in path.parents):
            raise VideoTemplateError("video_original_distribution_rejected")
    details = path.stat()
    if not stat.S_ISREG(details.st_mode):
        raise VideoTemplateError("video_regular_file_required")
    if template and details.st_nlink != 1:
        raise VideoTemplateError("video_template_hardlink_rejected")
    if template and not details.st_mode & stat.S_IWUSR:
        raise VideoTemplateError("video_template_read_only")


def _read(path: Path) -> tuple[bytes, _FileSnapshot]:
    with path.open("rb") as handle:
        before = _FileSnapshot.from_stat(os.fstat(handle.fileno()))
        if before.size > MAX_VIDEO_BYTES:
            raise VideoTemplateError("video_file_too_large")
        raw = handle.read(MAX_VIDEO_BYTES + 1)
        after = _FileSnapshot.from_stat(os.fstat(handle.fileno()))
    if len(raw) > MAX_VIDEO_BYTES:
        raise VideoTemplateError("video_file_too_large")
    if before != after or after != _FileSnapshot.from_stat(path.stat()):
        raise VideoStalePreviewError()
    return raw, after


def _decode(raw: bytes) -> tuple[str, str, bytes]:
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise VideoTemplateError("video_unsupported_encoding")
    encoding, bom = "utf-8", b""
    for prefix, codec in (
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if raw.startswith(prefix):
            encoding, bom = codec, prefix
            break
    try:
        content = raw[len(bom) :].decode(encoding)
    except UnicodeError:
        raise VideoTemplateError("video_unsupported_encoding") from None
    if "\x00" in content:
        raise VideoTemplateError("video_unsupported_encoding")
    return content, encoding, bom


def _capture(content: str, policy: VideoReplacementPolicy) -> tuple[re.Match[str], re.Match[str]]:
    matches = []
    for name, pattern in _PATTERNS.items():
        match = pattern.search(content)
        if match is None:
            raise VideoTemplateError("video_missing_or_invalid_" + name.lower())
        if policy == "fields" and content.count('"' + name + '"') != 1:
            raise VideoTemplateError("video_ambiguous_" + name.lower())
        if len(match.group(1)) > MAX_IDENTIFIER_CHARS:
            raise VideoTemplateError("video_identifier_too_long")
        matches.append(match)
    return matches[0], matches[1]


def _bounded_replace(content: str, old: str, new: str) -> str:
    predicted_size = len(content) + content.count(old) * (len(new) - len(old))
    if predicted_size > MAX_VIDEO_BYTES:
        raise VideoTemplateError("video_output_too_large")
    return content.replace(old, new)


def preview_video_template(
    template_path: str | Path,
    steam_config_path: str | Path,
    policy: VideoReplacementPolicy = "fields",
    *,
    protected_roots: tuple[str | Path, ...] = (),
) -> VideoPreview:
    """Read two explicit files and return an immutable proposal without writing.

    ``after_*`` reports the actual rendered IDs, which can differ from ``source_*``
    in legacy mode because both replacements operate on the whole template.
    UTF-8 (optional BOM) and BOM-marked UTF-16 LE/BE are supported.
    """
    if policy not in ("fields", "legacy"):
        raise VideoTemplateError("video_unknown_policy")
    try:
        template = _absolute(template_path)
        source = _absolute(steam_config_path)
        roots = tuple(_absolute(root) for root in protected_roots)
        _check_path(template, template=True, protected_roots=roots)
        _check_path(source, template=False, protected_roots=roots)
        if template.samefile(source):
            raise VideoTemplateError("video_source_is_template")
        template_raw, template_snapshot = _read(template)
        source_raw, source_snapshot = _read(source)
    except VideoTemplateError:
        raise
    except ValueError:
        raise VideoTemplateError("video_invalid_path") from None
    except OSError:
        raise VideoTemplateError("video_file_read_failed") from None
    content, encoding, bom = _decode(template_raw)
    source_content, _, _ = _decode(source_raw)
    vendor, device = _capture(content, policy)
    source_vendor, source_device = _capture(source_content, policy)
    old_vendor, old_device = vendor.group(1), device.group(1)
    new_vendor, new_device = source_vendor.group(1), source_device.group(1)
    changed = old_vendor != new_vendor or old_device != new_device
    rendered = content
    if changed:
        if policy == "legacy":
            rendered = _bounded_replace(rendered, old_vendor, new_vendor)
            rendered = _bounded_replace(rendered, old_device, new_device)
        else:
            # Work backwards so the earlier capture's offsets stay valid.
            for match, value in sorted(
                ((vendor, new_vendor), (device, new_device)),
                key=lambda item: item[0].start(1),
                reverse=True,
            ):
                start, end = match.span(1)
                rendered = rendered[:start] + value + rendered[end:]
    if len(bom + rendered.encode(encoding)) > MAX_VIDEO_BYTES:
        raise VideoTemplateError("video_output_too_large")
    actual_vendor, actual_device = _capture(rendered, "legacy")
    return VideoPreview(
        template_path=template,
        steam_config_path=source,
        policy=policy,
        before_vendor_id=old_vendor,
        before_device_id=old_device,
        source_vendor_id=new_vendor,
        source_device_id=new_device,
        after_vendor_id=actual_vendor.group(1),
        after_device_id=actual_device.group(1),
        changed=changed,
        rendered_content=rendered,
        template_sha256=hashlib.sha256(template_raw).hexdigest(),
        steam_config_sha256=hashlib.sha256(source_raw).hexdigest(),
        encoding=encoding,
        protected_roots=roots,
        _bom=bom,
        _template_bytes=template_raw,
        _template_snapshot=template_snapshot,
        _steam_snapshot=source_snapshot,
    )


def _verify(preview: VideoPreview) -> None:
    try:
        current = preview_video_template(
            preview.template_path,
            preview.steam_config_path,
            preview.policy,
            protected_roots=preview.protected_roots,
        )
    except VideoTemplateError:
        raise VideoStalePreviewError() from None
    if current != preview:
        raise VideoStalePreviewError()


def _discard_staging(path: Path) -> None:
    # A leftover staging file must not replace the primary failure or conceal
    # the retained backup/rollback result. Locked temporary files may remain.
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _stage(path: Path, raw: bytes, mode: int, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=suffix, dir=path.parent
    )
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(mode)
        return staged
    except BaseException:
        _discard_staging(staged)
        raise


def _restore(preview: VideoPreview, rendered: bytes) -> bool:
    """Restore only our own replacement; never overwrite a concurrent third value."""
    staged = None
    try:
        _check_path(preview.template_path, template=True, protected_roots=preview.protected_roots)
        raw, _ = _read(preview.template_path)
        if raw == preview._template_bytes:
            return True
        if raw != rendered:
            return False
        staged = _stage(
            preview.template_path, preview._template_bytes, preview._template_snapshot.mode, ".tmp"
        )
        os.replace(staged, preview.template_path)
        return True
    except OSError, VideoTemplateError:
        return False
    finally:
        if staged is not None:
            _discard_staging(staged)


def apply_video_template(preview: VideoPreview) -> VideoApplyResult:
    """Verify the proposal, retain a unique backup and atomically replace the copy.

    No-op proposals still undergo stale checks but create no file. The local lock
    serializes this service's writers; it cannot lock out external applications.
    """
    with _LOCK:
        _verify(preview)
        if not preview.changed:
            return VideoApplyResult(False, preview.template_path, None)
        rendered = preview._bom + preview.rendered_content.encode(preview.encoding)
        staged = None
        backup = None
        attempted_replace = False
        try:
            staged = _stage(
                preview.template_path, rendered, preview._template_snapshot.mode, ".tmp"
            )
            backup = _stage(
                preview.template_path,
                preview._template_bytes,
                preview._template_snapshot.mode,
                ".bak",
            )
            _verify(preview)
            attempted_replace = True
            os.replace(staged, preview.template_path)
        except VideoStalePreviewError:
            if backup is not None:
                _discard_staging(backup)
            raise
        except OSError:
            restored = not attempted_replace or _restore(preview, rendered)
            raise VideoApplyError(backup, rollback_succeeded=restored) from None
        finally:
            if staged is not None:
                _discard_staging(staged)
        return VideoApplyResult(True, preview.template_path, backup)
