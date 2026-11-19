"""Native tools receive literal argv; the runner never requests a shell."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .model import DistroError, expand


def tool(name):
    if name == "{python}":
        return sys.executable
    return shutil.which(name)


@dataclass
class Runner:
    timeout: int = 600
    records: list = field(default_factory=list)

    def run(self, argv, cwd, env=None):
        argv = [str(arg) for arg in argv]
        environment = os.environ.copy()
        environment.update(env or {})
        start = time.monotonic()
        try:
            result = subprocess.run(argv, cwd=cwd, env=environment, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=self.timeout, shell=False, errors="replace")
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DistroError(f"cannot run {argv[0]}: {error}") from error
        record = {"argv": argv, "cwd": str(cwd), "returncode": result.returncode,
                  "seconds": round(time.monotonic() - start, 4), "output": result.stdout[-12000:]}
        self.records.append(record)
        if result.returncode:
            raise DistroError(f"{Path(argv[0]).name} exited {result.returncode}:\n{result.stdout[-5000:]}")
        return result.stdout

    def commands(self, rows, variables, cwd, env=None):
        for row in rows:
            self.run([expand(arg, variables) for arg in row], cwd, env)
