"""The UI stylesheet is balanced (N1, the Schedule styles that never matched).

`ui/src/index.css` once left `.spin {` open. Native CSS nesting makes that
valid CSS, so the build accepted it and every rule after it compiled as a
descendant of `.spin`: `.spin .schedule-screen`, `.spin .schedule-row`, and the
only mobile media query. None of them could ever match, and nothing noticed,
because nothing checks the source stylesheet. This does.

The rule is structural, not a grep for the old selector: every `{` must be
closed, and depth may never go negative. Nesting is not used in this project,
so a rule opened inside another plain rule is also refused.
"""
import re
from pathlib import Path

UI_SRC = Path(__file__).resolve().parent.parent / "ui" / "src"


def _strip(css: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return re.sub(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", '""', css)


def _problems(css: str) -> list:
    problems = []
    stack = []  # one entry per open block: True when it is an @-rule
    prelude = []
    for index, char in enumerate(_strip(css)):
        if char == "{":
            head = "".join(prelude).strip()
            if stack and not stack[-1] and not head.startswith("@"):
                problems.append(f"'{head}' is nested inside a plain rule")
            stack.append(head.startswith("@"))
            prelude = []
        elif char == "}":
            if not stack:
                problems.append(f"a '}}' at offset {index} closes nothing")
            else:
                stack.pop()
            prelude = []
        elif char == ";":
            prelude = []
        else:
            prelude.append(char)
    if stack:
        problems.append(f"{len(stack)} block(s) are never closed")
    return problems


def test_every_ui_stylesheet_is_balanced_and_flat():
    sheets = sorted(UI_SRC.rglob("*.css"))
    assert sheets, f"no stylesheets under {UI_SRC}"
    report = {str(p.relative_to(UI_SRC)): _problems(p.read_text()) for p in sheets}
    broken = {name: found for name, found in report.items() if found}
    assert not broken, (
        "a stylesheet is structurally broken, so every rule after the fault "
        f"silently stops matching: {broken}")


def test_the_checker_sees_an_unclosed_rule():
    assert _problems(".spin {\n  color: red;\n\n.schedule-row { color: blue; }\n")
    assert not _problems("@media (max-width: 1px) { .a { color: red; } }")
