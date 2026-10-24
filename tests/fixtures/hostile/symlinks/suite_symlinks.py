import json
import os

LINKS = ["plain_passwd", "plain_sock", "real_passwd", "real_ssh", "real_sock", "real_rel"]


def test_read_links():
    out = {}
    for n in LINKS:
        p = os.path.join("/workspace", n)
        try:
            with open(p, "rb") as f:
                out[n] = ["read", os.path.islink(p), f.read(300).decode("utf-8", "replace")]
        except OSError as e:
            out[n] = ["err", os.path.islink(p), e.errno]
    with open("/out/links.json", "w") as f:
        json.dump(out, f)
