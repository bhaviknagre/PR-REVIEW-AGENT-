def test_write_evil_artifacts():
    laughs = ['<!DOCTYPE l [<!ENTITY a0 "lol">']
    for i in range(1, 9):
        laughs.append(f'<!ENTITY a{i} "' + f"&a{i - 1};" * 10 + '">')
    laughs.append("]>")
    bomb = "".join(laughs) + '<testsuite><testcase classname="c" name="&a8;"/></testsuite>'
    xxe = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        '<testsuite><testcase classname="c" name="&e;">'
        '<failure message="&e;"/></testcase></testsuite>'
    )
    cobertura = (
        "<coverage><packages><package><classes>"
        '<class filename="../../../etc/passwd"><lines><line number="1" hits="1"/></lines></class>'
        '<class filename="/etc/shadow"><lines><line number="2" hits="0"/></lines></class>'
        '<class filename="ok.py"><lines><line number="3" hits="1"/></lines></class>'
        "</classes></package></packages></coverage>"
    )
    lcov = "SF:../../.ssh/id_rsa\nDA:1,1\nend_of_record\nSF:/etc/shadow\nDA:2,0\nend_of_record\n"
    for name, body in (
        ("bomb.xml", bomb),
        ("xxe.xml", xxe),
        ("cob.xml", cobertura),
        ("trav.lcov", lcov),
    ):
        with open("/out/" + name, "w") as f:
            f.write(body)
