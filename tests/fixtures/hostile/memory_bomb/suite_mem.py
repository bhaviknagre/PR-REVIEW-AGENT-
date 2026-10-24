def test_mem():
    hog = []
    while True:
        hog.append(bytearray(32 << 20))
        for b in hog:
            b[::4096] = b"x" * len(b[::4096])
