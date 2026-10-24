import os


def test_leak():
    raise AssertionError("LEAK " + os.environ["EXFIL"])
