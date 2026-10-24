import ctypes
import json
import os


def attempt(fn):
    try:
        fn()
        return "OK"
    except OSError as e:
        return f"errno {e.errno}"


def test_privileged():
    libc = ctypes.CDLL(None, use_errno=True)

    def c(rc):
        if rc != 0:
            e = ctypes.get_errno()
            raise OSError(e, "x")

    def wr(path):
        def f():
            with open(path, "w") as h:
                h.write("1")

        return f

    def suid():
        p = "/workspace/suite_privileged.py"
        os.chmod(p, 0o4755)

    def ptrace():
        libc.ptrace.restype = ctypes.c_long
        r = libc.ptrace(16, 1, 0, 0)  # PTRACE_ATTACH pid 1
        if r == -1:
            raise OSError(ctypes.get_errno(), "x")

    res = {
        "mount": attempt(lambda: c(libc.mount(b"tmpfs", b"/tmp", b"tmpfs", 0, None))),
        "unshare_user": attempt(lambda: c(libc.unshare(0x10000000))),
        "unshare_mount": attempt(lambda: c(libc.unshare(0x20000))),
        "ptrace_pid1": attempt(ptrace),
        "chmod_suid_ro": attempt(suid),
        "write_sys": attempt(wr("/sys/kernel/uevent_helper")),
        "write_proc_sys": attempt(wr("/proc/sys/kernel/hostname")),
        "write_workspace": attempt(wr("/workspace/pwned")),
        "setuid0": attempt(lambda: os.setuid(0)),
    }
    open("/tmp/s", "w").close()
    os.chmod("/tmp/s", 0o4755)
    res["tmp_mount_opts"] = [
        line.split()[5] for line in open("/proc/self/mountinfo") if line.split()[4] == "/tmp"
    ]
    with open("/out/priv.json", "w") as f:
        json.dump(res, f)
