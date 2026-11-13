"""The scripts of web/ are classic scripts: they share one global scope, and a top-level
`function f` declared in two files is not an error. The file loaded last wins, silently, for
every caller. On 2026-11-13 review.js's `openInDev(f)` replaced app.js's `openInDev(id)`, so a
double-click on a card in Organize mode opened the file "undefined" instead of the task."""

import re
from collections import defaultdict
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "src" / "brain_fry" / "web"
TOP = re.compile(r"^(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)|^(?:const|let|var|class)\s+([A-Za-z_$][\w$]*)", re.M)


def test_no_top_level_name_in_two_scripts():
    where = defaultdict(set)
    for js in WEB.glob("*.js"):
        for m in TOP.finditer(js.read_text()):
            where[m.group(1) or m.group(2)].add(js.name)
    dup = {name: sorted(files) for name, files in where.items() if len(files) > 1}
    assert not dup, f"declared at top level in more than one script of web/: {dup}"
