import socket


def test_net():
    s = socket.socket()
    s.settimeout(3)
    s.connect(("1.1.1.1", 53))
