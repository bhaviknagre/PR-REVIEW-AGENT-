import sys
import time


def test_loris():
    while True:
        sys.__stdout__.write("x")
        sys.__stdout__.flush()
        time.sleep(1)
