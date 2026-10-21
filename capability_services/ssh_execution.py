"""Execution contract shared by the two SSH capability gateways."""
from __future__ import annotations

from contextlib import suppress
import math
import os
import signal
import subprocess
import tempfile
import threading
from typing import Any, BinaryIO

MAX_BODY = 8 * 1024 * 1024
MAX_COMMAND = 1024 * 1024  # Unicode characters, matching JSON Schema maxLength.
MAX_OUTPUT = 16 * 1024 * 1024  # Raw bytes per output stream.
DEFAULT_TIMEOUT = 300.0
MAX_TIMEOUT = 3600.0


class GatewayError(Exception):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def validate_exec(command: Any, timeout_seconds: Any) -> float:
    if (not isinstance(command, str) or not command.strip()
            or len(command) > MAX_COMMAND or "\x00" in command):
        raise GatewayError("command_invalid")
    if (isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 1 <= timeout_seconds <= MAX_TIMEOUT):
        raise GatewayError("timeout_invalid")
    return float(timeout_seconds)


def execute_ssh(args: list[str], command: str, timeout: float) -> dict[str, Any]:
    outputs = [bytearray(), bytearray()]
    truncated = [False, False]

    def drain(stream: BinaryIO, index: int) -> None:
        with stream:
            while chunk := stream.read(64 * 1024):
                remaining = MAX_OUTPUT - len(outputs[index])
                outputs[index].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated[index] = True

    # A file-backed stdin avoids both argv limits and a blocked stdin writer
    # while stdout/stderr are being produced. Only the script is spooled.
    with tempfile.TemporaryFile() as script:
        script.write(command.encode("utf-8") + b"\n")
        script.seek(0)
        try:
            process = subprocess.Popen(
                [*args, "sh -s"], stdin=script,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise GatewayError("ssh_unavailable", 503) from exc
        assert process.stdout is not None and process.stderr is not None
        readers = [threading.Thread(target=drain, args=(stream, index))
                   for index, stream in enumerate((process.stdout, process.stderr))]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        finally:
            for reader in readers:
                reader.join()
    return {
        "stdout": outputs[0].decode("utf-8", errors="replace"),
        "stderr": outputs[1].decode("utf-8", errors="replace"),
        "exit_code": None if timed_out else process.returncode,
        "timed_out": timed_out,
        "truncated": any(truncated),
    }
