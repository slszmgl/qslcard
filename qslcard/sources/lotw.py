"""ARRL Logbook of The World via the local TQSL CLI (SRS 4.4).

LoTW has no general public REST API, so the only automated path is the TQSL
command line.  Decision D-01 allows the program to call TQSL and to use the
user certificate password, on the strict condition that the password is passed
through the child process environment, never on the command line, never logged
and never written to disk (SR-LOTW-009/010).

The exact TQSL switches vary between releases, so the two command templates are
configuration, not hard-coded assumptions.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..adif import iter_qsos
from ..model import QSO
from ..net import TransportError
from .base import CallbookRecord, SourceContext

__all__ = ["LotwSource", "TqslResult", "find_tqsl", "run_tqsl"]

_TQSL_CANDIDATES = (
    os.path.expandvars(r"%ProgramFiles(x86)%\TrustedQSL\tqsl.exe"),
    os.path.expandvars(r"%ProgramFiles%\TrustedQSL\tqsl.exe"),
    "/Applications/TrustedQSL/tqsl.app/Contents/MacOS/tqsl",
    "/usr/bin/tqsl",
    "/usr/local/bin/tqsl",
)

DEFAULT_DOWNLOAD_COMMAND = ("tqsl", "-d", "-a", "-o", "{adi}")
DEFAULT_UPLOAD_COMMAND = ("tqsl", "-u", "-a", "{adi}")


@dataclass(slots=True)
class TqslResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


def find_tqsl(explicit: str = "") -> str | None:
    if explicit:
        return explicit if Path(explicit).is_file() else None
    found = shutil.which("tqsl") or shutil.which("tqsl.exe")
    if found:
        return found
    for candidate in _TQSL_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def run_tqsl(
    command: Sequence[str],
    *,
    password: str = "",
    password_env: str = "TQSL_PASSWORD",
    runner: object = None,
    timeout: float = 600.0,
) -> TqslResult:
    """Run TQSL with the certificate password supplied out-of-band.

    The password is placed in the child environment only: it never appears in
    argv, so it cannot leak through process listings or shell history.
    """
    argv = [str(part) for part in command]
    if password and any(password in part for part in argv):
        raise ValueError("refusing to pass the certificate password on the command line")
    env = dict(os.environ)
    if password:
        env[password_env] = password
    runner = runner or subprocess.run
    completed = runner(
        argv,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return TqslResult(
        command=tuple(argv),
        returncode=int(getattr(completed, "returncode", 0)),
        stdout=str(getattr(completed, "stdout", "") or ""),
        stderr=str(getattr(completed, "stderr", "") or ""),
    )


class LotwSource:
    name = "lotw"

    def __init__(self, tqsl_path: str = "") -> None:
        self.tqsl_path = tqsl_path

    # -- configuration ---------------------------------------------------
    def _executable(self, ctx: SourceContext) -> str:
        configured = self.tqsl_path or str(ctx.option("tqsl_path", ""))
        found = find_tqsl(configured)
        if not found:
            raise TransportError(
                "TQSL was not found. Install TrustedQSL from ARRL and set lotw.tqsl_path "
                "in the configuration; LoTW has no public API, so TQSL is the only path."
            )
        return found

    def _command(self, ctx: SourceContext, kind: str, adi_path: str) -> list[str]:
        template = ctx.option(f"{kind}_command") or (
            DEFAULT_DOWNLOAD_COMMAND if kind == "download" else DEFAULT_UPLOAD_COMMAND
        )
        parts = [str(part).format(adi=adi_path) for part in template]
        if parts and parts[0] in {"tqsl", "tqsl.exe"}:
            parts[0] = self._executable(ctx)
        return parts

    def is_configured(self, ctx: SourceContext) -> bool:
        return find_tqsl(self.tqsl_path or str(ctx.option("tqsl_path", ""))) is not None

    # -- operations ------------------------------------------------------
    def download(self, ctx: SourceContext, adi_path: str) -> TqslResult:
        command = self._command(ctx, "download", adi_path)
        result = run_tqsl(
            command,
            password=ctx.credential("lotw_password"),
            password_env=str(ctx.option("password_env", "TQSL_PASSWORD")),
            runner=ctx.runner,
        )
        if result.returncode != 0:
            raise TransportError(
                "TQSL download failed "
                f"(exit {result.returncode}): {result.stderr.strip()[:200] or 'no diagnostics'}"
            )
        return result

    def upload(self, ctx: SourceContext, adi_path: str) -> TqslResult:
        command = self._command(ctx, "upload", adi_path)
        result = run_tqsl(
            command,
            password=ctx.credential("lotw_password"),
            password_env=str(ctx.option("password_env", "TQSL_PASSWORD")),
            runner=ctx.runner,
        )
        if result.returncode != 0:
            raise TransportError(
                "TQSL upload failed "
                f"(exit {result.returncode}): {result.stderr.strip()[:200] or 'no diagnostics'}"
            )
        return result

    def sync(self, ctx: SourceContext, work_dir: str) -> list[QSO]:
        """Download confirmations with TQSL and return them as QSOs.

        This is the automated counterpart of fetch(): it produces the ADIF that
        the LoTW website would otherwise have to export by hand.
        """
        target = Path(work_dir) / "lotw-download.adi"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        self.download(ctx, str(target))
        if not target.is_file():
            raise TransportError(
                "TQSL reported success but wrote no ADIF file; check that the "
                "download command writes to the {adi} path in lotw.download_command"
            )
        confirmations: list[QSO] = []
        for qso in iter_qsos(target):
            qso.source = self.name
            qso.lotw_qsl_rcvd = qso.lotw_qsl_rcvd or "Y"
            confirmations.append(qso)
        return confirmations

    def fetch(self, ctx: SourceContext, *, since: str = "") -> Iterator[QSO]:
        """Yield confirmations from a LoTW ADIF report.

        Either point lotw.report_path at a file exported from the LoTW website,
        or call download() first to produce one with TQSL.
        """
        report = str(ctx.option("report_path", ""))
        if not report:
            return
        if not Path(report).is_file():
            raise TransportError(f"LoTW report not found: {report}")
        for qso in iter_qsos(report):
            qso.source = self.name
            qso.lotw_qsl_rcvd = qso.lotw_qsl_rcvd or "Y"
            yield qso

    def lookup(self, calls: Sequence[str], ctx: SourceContext) -> list[CallbookRecord]:
        return []

    def test(self, ctx: SourceContext) -> str:
        executable = self._executable(ctx)
        return f"TQSL found at {executable}"
