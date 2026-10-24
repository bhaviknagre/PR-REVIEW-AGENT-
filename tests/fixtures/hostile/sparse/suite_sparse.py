def test_sparse():
    try:
        with open("/out/big.bin", "wb") as f:
            f.truncate(10 << 30)
    except OSError:
        pass
    with open("/out/big2.bin", "wb") as f:
        f.seek((10 << 30) - 1)
        f.write(b"x")
