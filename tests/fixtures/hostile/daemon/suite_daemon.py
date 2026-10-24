import os
import subprocess
import sys


def test_spawn_daemons():
    subprocess.Popen(["sleep", "600"], start_new_session=True, stdout=subprocess.DEVNULL)
    subprocess.Popen(
        [sys.executable, "-c", "import time\nwhile True: time.sleep(1)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    if os.fork() == 0:  # orphaned double-fork that outlives pytest
        os.setsid()
        while True:
            os.system("sleep 1")
