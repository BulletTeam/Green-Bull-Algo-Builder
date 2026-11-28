# একক-ফাইল বিল্ডার তৈরি: src/py/*.py (মডিউল, নামের ক্রমে) + src/ui.html → ../algo_builder.py
#   cd builder/src && python make.py
import glob, os
H = os.path.dirname(os.path.abspath(__file__))
tpl = "".join(open(p, encoding="utf-8").read() for p in sorted(glob.glob(os.path.join(H, "py", "*.py"))))
html = open(os.path.join(H, "ui.html"), encoding="utf-8").read()
assert "'''" not in html, "HTML-এ ''' আছে"
out = tpl.replace("@@HTML@@", html)
assert "@@HTML@@" not in out
open(os.path.join(H, "..", "algo_builder.py"), "w", encoding="utf-8").write(out)
print("built", len(out))
