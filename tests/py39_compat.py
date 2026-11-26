"""Detect f-string constructs that Python 3.12+ accepts but Rocky 9's Python 3.9 rejects:
a quote of the same kind, or a backslash, inside an f-string replacement field.
Usage: python tests/py39_compat.py [paths...]   (needs Python 3.12+ to run)"""
import pathlib, sys, tokenize

def check(path):
    bad = []
    with open(path, "rb") as f:
        toks = list(tokenize.tokenize(f.readline))
    stack = []  # (quote char, depth of braces)
    for t in toks:
        if t.type == tokenize.FSTRING_START:
            q = t.string.lstrip("rRfFbBuU")[0]
            if stack and q == stack[-1]:
                bad.append((t.start[0], "nested f-string reuses enclosing quote"))
            stack.append(q)
        elif t.type == tokenize.FSTRING_END:
            stack.pop()
        elif stack and t.type == tokenize.STRING:
            q = t.string.lstrip("rRfFbBuU")[0]
            if q in stack:
                bad.append((t.start[0], "string inside f-string reuses enclosing quote"))
            if "\\" in t.string:
                bad.append((t.start[0], "backslash inside f-string expression"))
    return bad

paths = sys.argv[1:] or ["src", "installer", "build/build.py", "build/repotool.py", "build/fatimg.py", "packaging/bin"]
n = 0
for root in paths:
    rp = pathlib.Path(root)
    files = [rp] if rp.is_file() else [p for p in rp.rglob("*") if p.suffix == ".py" or (p.parent.name == "bin" and p.is_file())]
    for p in files:
        for line, msg in check(p):
            print(f"{p}:{line}: {msg}")
            n += 1
print(f"{n} problem(s)")
sys.exit(1 if n else 0)
