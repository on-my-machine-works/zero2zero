"""Declarative offload job hook runner (`/etc/zero2zero/jobs.d/*.json`).

Allows Flipper Zero or CardputerZero UI/CLI to trigger arbitrary local Kali Linux
CLI pipelines — pulling input files from `/ext/`, executing in an isolated workspace,
merging/pushing result files back over BLE, and optionally alerting Flipper on completion.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
from typing import Any, ContextManager, Dict, List, Optional

from .rpc_client import FlipperRpcClient


def _validate_flipper_path(remote: str) -> str:
    p = PurePosixPath(remote)
    if not p.is_absolute() or ".." in p.parts or p.parts[1:2] not in (("ext",), ("int",)):
        raise ValueError(f"Invalid Flipper storage path: {remote!r}")
    return str(p)


def _validate_rel_filename(name: str) -> str:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"Workspace filename must be a simple basename: {name!r}")
    return name


@dataclass
class JobSpec:
    id: str
    title: str
    description: str = ""
    command: List[str] = field(default_factory=list)
    pull_files: List[Dict[str, object]] = field(default_factory=list)
    push_files: List[Dict[str, object]] = field(default_factory=list)
    merge_lines_into: Optional[Dict[str, str]] = None
    timeout_sec: int = 30
    alert_on_finish: bool = False
    source_path: str = ""

    def to_summary(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "timeout_sec": self.timeout_sec,
            "alert_on_finish": self.alert_on_finish,
        }


def job_search_dirs() -> List[Path]:
    dirs: List[Path] = []
    env_dirs = os.environ.get("Z2Z_JOBS_DIR")
    if env_dirs:
        for part in env_dirs.split(":"):
            if part.strip():
                dirs.append(Path(part.strip()))
    repo_etc = Path(__file__).resolve().parents[2] / "etc" / "jobs.d"
    if repo_etc.is_dir():
        dirs.append(repo_etc)
    dirs.append(Path("/etc/zero2zero/jobs.d"))
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    dirs.append(Path(xdg) / "zero2zero" / "jobs.d")
    return dirs


def load_jobs(extra_dirs: Optional[List[Path]] = None) -> Dict[str, JobSpec]:
    jobs: Dict[str, JobSpec] = {}
    search = list(extra_dirs) if extra_dirs is not None else job_search_dirs()
    for d in search:
        if not d.is_dir():
            continue
        for jf in sorted(d.glob("*.json")):
            try:
                raw = json.loads(jf.read_text(encoding="utf-8"))
                jid = str(raw["id"]).strip()
                if not jid:
                    continue
                jobs[jid] = JobSpec(
                    id=jid,
                    title=str(raw.get("title", jid)),
                    description=str(raw.get("description", "")),
                    command=[str(x) for x in raw.get("command", [])],
                    pull_files=list(raw.get("pull_files", [])),
                    push_files=list(raw.get("push_files", [])),
                    merge_lines_into=raw.get("merge_lines_into"),
                    timeout_sec=int(raw.get("timeout_sec", 30)),
                    alert_on_finish=bool(raw.get("alert_on_finish", False)),
                    source_path=str(jf),
                )
            except Exception:
                continue
    return jobs


def _resolve_latest_in_dirs(
    rpc: FlipperRpcClient,
    dirs: List[str],
    suffixes: List[str],
) -> Optional[str]:
    norm_sfx = tuple(s.lower() for s in suffixes if s)
    for raw_d in dirs:
        d = _validate_flipper_path(raw_d)
        entries = rpc.storage_list(d, include_md5=False)
        candidates = [
            e.name
            for e in entries
            if not e.is_dir
            and e.name
            and not e.name.startswith(".")
            and (not norm_sfx or e.name.lower().endswith(norm_sfx))
        ]
        if candidates:
            candidates.sort()
            return str(PurePosixPath(d) / candidates[-1])
    return None


def execute_job(
    job: JobSpec,
    rpc: Optional[FlipperRpcClient] = None,
    extra_env: Optional[Dict[str, str]] = None,
    rpc_lock: Optional[ContextManager[Any]] = None,
) -> Dict[str, object]:
    if not job.command:
        raise ValueError(f"Job {job.id!r} defines an empty command")

    pulled: List[str] = []
    pushed: List[str] = []
    merged_added = 0
    lock_cm: ContextManager[Any] = rpc_lock if rpc_lock is not None else nullcontext()

    with tempfile.TemporaryDirectory(prefix=f"z2z-job-{job.id}-") as tmp:
        workdir = Path(tmp)

        # 1. Pull requested input files from Flipper `/ext/...`
        if job.pull_files:
            with lock_cm:
                for item in job.pull_files:
                    optional = bool(item.get("optional", False))
                    if rpc is None or not rpc.transport.is_connected:
                        if optional:
                            continue
                        raise RuntimeError(
                            f"Job {job.id!r} requires an active Flipper BLE connection"
                        )

                    if "remote_dir" in item or "remote_dirs" in item:
                        raw_dirs = (
                            list(item["remote_dirs"])  # type: ignore[arg-type]
                            if "remote_dirs" in item
                            else [str(item["remote_dir"])]
                        )
                        raw_sfx = (
                            list(item["suffixes"])  # type: ignore[arg-type]
                            if "suffixes" in item
                            else ([str(item["suffix"])] if item.get("suffix") else [])
                        )
                        resolved = _resolve_latest_in_dirs(rpc, raw_dirs, raw_sfx)
                        if resolved is None:
                            if optional:
                                continue
                            raise FileNotFoundError(
                                f"No matching {raw_sfx or 'capture'} files found in {raw_dirs}"
                            )
                        remote = resolved
                    else:
                        remote = _validate_flipper_path(str(item["remote"]))
                        st = rpc.storage_stat(remote)
                        if st is None:
                            if optional:
                                continue
                            raise FileNotFoundError(f"Missing required file on Flipper: {remote}")

                    local_name = _validate_rel_filename(
                        str(item.get("local") or os.path.basename(remote))
                    )
                    (workdir / local_name).write_bytes(rpc.storage_read(remote))
                    pulled.append(remote)

        # 2. Execute local Kali command in temporary workdir (without holding BLE RPC lock)
        env = os.environ.copy()
        py_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = py_root + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["Z2Z_WORKDIR"] = str(workdir)
        if pulled:
            env["Z2Z_PULLED_REMOTE"] = pulled[-1]
        if extra_env:
            for k, v in extra_env.items():
                env[f"Z2Z_ARG_{k.upper()}"] = str(v)

        proc = subprocess.run(
            job.command,
            cwd=str(workdir),
            env=env,
            capture_output=True,
            text=True,
            timeout=max(1, job.timeout_sec),
        )

        # 3. Push output files on success
        if proc.returncode == 0 and rpc is not None and (
            job.push_files or job.merge_lines_into or job.alert_on_finish
        ):
            with lock_cm:
                if rpc.transport.is_connected:
                    for item in job.push_files:
                        local_name = _validate_rel_filename(str(item["local"]))
                        remote = _validate_flipper_path(str(item["remote"]))
                        lpath = workdir / local_name
                        if lpath.is_file():
                            parent = str(PurePosixPath(remote).parent)
                            rpc.storage_mkdirs(parent)
                            rpc.storage_write(remote, lpath.read_bytes())
                            pushed.append(remote)

                    if job.merge_lines_into:
                        local_name = _validate_rel_filename(str(job.merge_lines_into["local"]))
                        remote = _validate_flipper_path(str(job.merge_lines_into["remote"]))
                        lpath = workdir / local_name
                        if lpath.is_file():
                            new_lines = [
                                ln.strip()
                                for ln in lpath.read_text(
                                    encoding="utf-8", errors="replace"
                                ).splitlines()
                                if ln.strip() and not ln.strip().startswith("#")
                            ]
                            existing_lines: List[str] = []
                            if rpc.storage_stat(remote) is not None:
                                raw_old = rpc.storage_read(remote).decode(
                                    "utf-8", errors="replace"
                                )
                                existing_lines = [ln.rstrip("\r") for ln in raw_old.splitlines()]
                            seen = {
                                ln.strip()
                                for ln in existing_lines
                                if ln.strip() and not ln.strip().startswith("#")
                            }
                            for nl in new_lines:
                                if nl not in seen:
                                    existing_lines.append(nl)
                                    seen.add(nl)
                                    merged_added += 1
                            if merged_added > 0 or rpc.storage_stat(remote) is None:
                                parent = str(PurePosixPath(remote).parent)
                                rpc.storage_mkdirs(parent)
                                payload = (
                                    "\n".join(existing_lines).rstrip("\n") + "\n"
                                ).encode("utf-8")
                                rpc.storage_write(remote, payload)
                                pushed.append(remote)

                    if job.alert_on_finish:
                        try:
                            rpc.trigger_gentle_alert()
                        except Exception:
                            pass

        output_text = (proc.stdout or "").strip()
        if proc.stderr and proc.returncode != 0:
            err_tail = proc.stderr.strip()
            output_text = (output_text + "\n" + err_tail).strip()

        return {
            "job_id": job.id,
            "title": job.title,
            "returncode": proc.returncode,
            "output": output_text[:4096],
            "pulled": pulled,
            "pushed": pushed,
            "merged_added": merged_added,
        }
