import sys
tpl = open("algo_builder.tpl.py", encoding="utf-8").read()
html = open("ui.html", encoding="utf-8").read()
assert "'''" not in html, "HTML-এ ''' আছে"
assert not html.rstrip().endswith("\\")
out = tpl.replace("@@HTML@@", html)
assert "@@HTML@@" not in out
open("../algo_builder.py", "w", encoding="utf-8").write(out)
print("built", len(out))
