import time


def test_flood():
    end = time.monotonic() + 20
    for i in range(150_000):
        if time.monotonic() > end:
            break
        try:
            open(f"/out/f{i}", "w").write("x")
        except OSError:
            break
