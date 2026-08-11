"""Prepare conservative, messaging-app-compatible copies of camera videos."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path, PurePosixPath


class MobileVideoError(RuntimeError):
    """A phone-compatible copy could not be prepared."""


class MobileVideoPreparer:
    """Transcode camera MP4s once and cache the phone-compatible results."""

    def __init__(self, library_root: Path, *, ffmpeg: str = "ffmpeg") -> None:
        self.library_root = library_root.expanduser().resolve()
        self.media_root = self.library_root / "media"
        # Version the directory so future encoding changes never reuse stale files.
        self.output_root = self.library_root / "mobile-v2"
        self.ffmpeg = ffmpeg
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def prepare(self, relative_path: str) -> Path:
        resolved_source, destination = self._validated_paths(relative_path)

        key = PurePosixPath(relative_path).as_posix()
        with self._locks_guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            if self._is_current(resolved_source, destination):
                return destination.resolve()
            return self._transcode(resolved_source, destination)

    def cached(self, relative_path: str) -> Path | None:
        """Return a current prepared copy without creating or changing files."""
        resolved_source, destination = self._validated_paths(relative_path)
        if self._is_current(resolved_source, destination):
            return destination.resolve()
        return None

    def _validated_paths(self, relative_path: str) -> tuple[Path, Path]:
        relative = PurePosixPath(relative_path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.suffix.casefold() != ".mp4"
        ):
            raise MobileVideoError("invalid video path")

        source = self.media_root.joinpath(*relative.parts)
        destination = self.output_root.joinpath(*relative.parts)
        try:
            resolved_source = source.resolve(strict=True)
        except (FileNotFoundError, OSError) as error:
            raise MobileVideoError("source video is missing") from error
        if self.media_root not in resolved_source.parents or not resolved_source.is_file():
            raise MobileVideoError("video path escaped the media library")
        return resolved_source, destination

    @staticmethod
    def _is_current(source: Path, destination: Path) -> bool:
        try:
            return (
                destination.is_file()
                and destination.stat().st_size > 0
                # A prepared file inherits the exact source timestamp when it is
                # published. Equality is a durable source-version marker even
                # when the camera clock is ahead of the Pi clock; comparing
                # which file is "newer" caused every share to transcode again
                # for future-dated camera files.
                and destination.stat().st_mtime_ns == source.stat().st_mtime_ns
            )
        except OSError:
            return False

    def _transcode(self, source: Path, destination: Path) -> Path:
        executable = shutil.which(self.ffmpeg)
        if executable is None:
            raise MobileVideoError("ffmpeg is not installed")

        try:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o770)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.stem}.", suffix=".mp4", dir=destination.parent
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
        except OSError as error:
            raise MobileVideoError("video cache is not writable") from error
        command = [
            executable,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-fflags",
            "+discardcorrupt",
            "-err_detect",
            "ignore_err",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            "scale=w='min(1280,iw)':h=-2:flags=lanczos,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-profile:v",
            "main",
            "-level:v",
            "3.1",
            "-tag:v",
            "avc1",
            "-crf",
            "21",
            "-maxrate",
            "4M",
            "-bufsize",
            "8M",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            "96k",
            "-ar",
            "44100",
            "-movflags",
            "+faststart",
            "-brand",
            "mp42",
            str(temporary),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=600,
            )
            if completed.returncode != 0 or temporary.stat().st_size == 0:
                detail = completed.stderr.strip().splitlines()[-1:] or ["unknown error"]
                raise MobileVideoError(f"ffmpeg failed: {detail[0]}")
            os.replace(temporary, destination)
            source_mtime_ns = source.stat().st_mtime_ns
            os.utime(destination, ns=(source_mtime_ns, source_mtime_ns))
            # The LAN preparer and nightly downloader use different service
            # accounts, while the public gallery is read-only. Publish the
            # derivative to their shared birdfeeder group without exposing it
            # to unrelated local users.
            destination.chmod(0o660)
            return destination.resolve()
        except subprocess.TimeoutExpired as error:
            raise MobileVideoError("video preparation timed out") from error
        except OSError as error:
            raise MobileVideoError("video cache could not be published") from error
        finally:
            temporary.unlink(missing_ok=True)
