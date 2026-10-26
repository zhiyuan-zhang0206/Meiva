# meiva:note
# Classify one stored schedule script for `sched-dry-import-weekly`: read it on stdin and print
# exactly one line -- `CHILD-OK`, or the first reason it cannot be imported. This runs in the
# runner's interpreter with the checkout as cwd, so an import that only resolves inside the
# repository is still reported as missing.
#
# A real file rather than a `python -c` string: the classification is code like any other, and a
# change to it should be reviewable, importable and greppable instead of hiding in a literal.
# meiva:link schedules/README.md

import ast
import sys


# meiva:note
# The classification itself: compile the script, then execute only its import statements and report
# the first module that cannot be resolved. Exit codes name the failure class for the parent (3
# syntax, 4 missing module, 5 any other import-time error) so one red line can say which.
def main() -> None:
    src = sys.stdin.read()
    try:
        compile(src, "<schedule>", "exec")
        tree = ast.parse(src)
    except SyntaxError as exc:
        print("CHILD-COMPILE-ERROR:%s:%s" % (exc.lineno, exc.msg))
        sys.exit(3)
    segments: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            segment = ast.get_source_segment(src, node)
            if segment:
                segments.append(segment)
    try:
        exec(compile("\n".join(segments), "<imports-only>", "exec"), {"__name__": "dryimport"})
    except ModuleNotFoundError as exc:
        print("CHILD-MODULE:%s" % exc.name)
        sys.exit(4)
    except Exception as exc:
        print("CHILD-EXC:%s:%s" % (type(exc).__name__, str(exc)[:120].replace("\n", " ")))
        sys.exit(5)
    print("CHILD-OK")


if __name__ == "__main__":
    main()
