"""Run Python against a read-only copy of one task's working files."""

import ctypes as C
import errno
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Thread

TIMEOUT = 15
OUTPUT_LIMIT = 8192
EXTENDED_OUTPUT_LIMIT = 2 * 1024 * 1024
CONTROL_FILES = {"task.json", "answer.txt"}


def _python_command(scratch: Path) -> list[str]:
    script = str(Path(__file__).resolve())
    if sys.platform != "darwin":
        return [sys.executable, "-IBS", script]

    executable = Path(sys.executable).resolve()
    readable = (
        scratch,
        Path(sys.base_prefix).resolve(),
        Path("/System/Library"),
        Path("/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld"),
        Path("/usr/lib"),
    )
    subpaths = " ".join(f"(subpath {json.dumps(str(path))})" for path in readable)
    profile = f"""(version 1)
(deny default)
(import "system.sb")
(allow sysctl-read)
(allow file-read-metadata)
(deny file-write* network* process-fork)
(allow file-read* {subpaths} (literal {json.dumps(script)}))
(allow file-map-executable {subpaths})
(allow process-exec (literal {json.dumps(str(executable))}))
"""
    return ["/usr/bin/sandbox-exec", "-p", profile, str(executable), "-IBS", script]


def _capture_output(command, scratch, code):
    """Capture the complete output of successful executions within the hard quota."""
    captured = bytearray()
    exceeded = False

    with tempfile.TemporaryFile(dir="/tmp") as source:
        source.write(code.encode())
        source.seek(0)
        with subprocess.Popen(
            command, cwd=scratch, env={"LANG": "C.UTF-8"}, stdin=source,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        ) as process:
            def drain():
                nonlocal exceeded
                while chunk := process.stdout.read1(8192):
                    room = EXTENDED_OUTPUT_LIMIT - len(captured)
                    captured.extend(chunk[:room])
                    if len(chunk) > room:
                        exceeded = True
                        process.kill()
                        break

            reader = Thread(target=drain)
            reader.start()
            try:
                process.wait(timeout=TIMEOUT)
                status = f"Exit code {process.returncode}"
            except subprocess.TimeoutExpired:
                status = f"Python exceeded the {TIMEOUT}s time limit."
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                reader.join()
    if exceeded:
        status = f"Python exceeded the {EXTENDED_OUTPUT_LIMIT}-byte output limit. Reduce the output and retry."
    return f"{status}\n" + captured.decode(errors="replace")


def run_python(workspace: Path, code: str, *, extended: bool = False) -> str:
    if sys.platform not in {"linux", "darwin"}:
        return "Python execution requires Linux or macOS."

    def ignored_entries(directory, names):
        parent = Path(directory)
        ignored = set()
        for name in names:
            path = parent / name
            if (
                name in CONTROL_FILES
                or path.is_symlink()
                or not (path.is_file() or path.is_dir())
            ):
                ignored.add(name)
        return ignored

    # Some bind-mounted filesystems fail Landlock checks even with an allow rule.
    with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
        scratch = Path(temporary).resolve() / "workspace"
        shutil.copytree(workspace, scratch, ignore=ignored_entries)
        command = _python_command(scratch)
        if extended:
            return _capture_output(command, scratch, code)
        with tempfile.TemporaryFile(dir="/tmp") as output:
            try:
                result = subprocess.run(
                    command,
                    cwd=scratch,
                    env={"LANG": "C.UTF-8"},
                    input=code.encode(),
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=TIMEOUT,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return f"Python exceeded the {TIMEOUT}s time limit."
            output.seek(0)
            data = output.read(OUTPUT_LIMIT + 1)

    suffix = "\n[output truncated]" if len(data) > OUTPUT_LIMIT else ""
    return f"Exit code {result.returncode}\n{data[:OUTPUT_LIMIT].decode(errors='replace')}{suffix}"


def restrict_files(workspace: Path) -> None:
    if platform.machine() not in {"x86_64", "aarch64"}:
        raise RuntimeError("unsupported Landlock architecture")
    libc = C.CDLL(None, use_errno=True)

    def syscall(number, *args):
        result = libc.syscall(C.c_long(number), *args)
        if result < 0:
            raise OSError(C.get_errno(), "Landlock syscall failed")
        return result

    # Landlock ABI 3 adds the ability to deny truncate as well as write.
    if syscall(444, None, 0, 1) < 3:
        raise RuntimeError("Landlock ABI 3 or newer is required")
    access = C.c_uint64((1 << 15) - 1)
    ruleset = syscall(444, C.byref(access), C.sizeof(access), 0)

    class PathRule(C.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", C.c_uint64), ("parent_fd", C.c_int32)]

    read_file, read_dir = 1 << 2, 1 << 3

    def allow_read(path, rights):
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        try:
            syscall(445, ruleset, 1, C.byref(PathRule(rights, fd)), 0)
        finally:
            os.close(fd)

    try:
        for directory in {
            Path(sys.base_prefix) / "lib",
            Path("/usr/lib"),
            Path("/lib"),
        }:
            if directory.is_dir():
                allow_read(directory, read_file | read_dir)
        allow_read(workspace, read_file | read_dir)
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            raise OSError(C.get_errno(), "could not set no_new_privs")
        syscall(446, ruleset, 0)
    finally:
        os.close(ruleset)


def restrict_syscalls() -> None:
    seccomp = C.CDLL("libseccomp.so.2")
    seccomp.seccomp_init.argtypes = [C.c_uint32]
    seccomp.seccomp_init.restype = C.c_void_p
    seccomp.seccomp_rule_add.argtypes = [C.c_void_p, C.c_uint32, C.c_int, C.c_uint]
    seccomp.seccomp_load.argtypes = [C.c_void_p]
    seccomp.seccomp_release.argtypes = [C.c_void_p]
    seccomp.seccomp_syscall_resolve_name.argtypes = [C.c_char_p]
    context = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("could not create seccomp filter")

    blocked = (
        "socket socketpair fork vfork clone clone3 kill tkill tgkill pidfd_send_signal "
        "ptrace process_vm_readv process_vm_writev pidfd_getfd io_uring_setup "
        "chmod fchmod fchmodat fchmodat2 chown fchown lchown fchownat "
        "setxattr lsetxattr fsetxattr removexattr lremovexattr fremovexattr "
        "utime utimes futimesat utimensat"
    )
    try:
        for name in blocked.split():
            number = seccomp.seccomp_syscall_resolve_name(name.encode())
            if (
                seccomp.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0)
                < 0
            ):
                raise RuntimeError(f"could not restrict syscall {name}")
        if seccomp.seccomp_load(context) < 0:
            raise RuntimeError("could not load seccomp filter")
    finally:
        seccomp.seccomp_release(context)


if __name__ == "__main__":
    workdir = Path.cwd()
    source = sys.stdin.read()
    resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_LIMIT * 2, OUTPUT_LIMIT * 2))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (512 << 20, 512 << 20))
        try:
            restrict_syscalls()
            restrict_files(workdir)
        except (OSError, RuntimeError) as error:
            sys.exit(f"Python sandbox unavailable: {error}")
    sys.path.insert(0, str(workdir))
    exec(compile(source, "<task>", "exec"), {"__name__": "__main__"})  # noqa: S102 (sandboxed above)
