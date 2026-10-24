def test_ansi_token():
    tok = "gh" + "p_" + "aB3dE5gH7jK9mN1pQ3sT5vX7zA9cD1fG3hJ5"
    raise AssertionError("\x1b[31m\x1b[2J\x1b]0;pwn\x07\x00\x08bad " + tok + " end")
