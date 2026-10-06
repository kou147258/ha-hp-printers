"""The README's tables are well formed.

Documentation is the one artefact in this repository that no test touches, and
a broken table row is not a crash: it renders as a stray sentence with pipes
around it, on the page a user reads before installing. Two have now appeared
here -- one from a batch text replacement that merged two rows, one already
in the file before that -- and neither would have been caught by anything
running in CI.

So the check lives here. It compares each row against the column count of the
header of the table it belongs to, which leaves every table free to have its
own width, and it skips fenced code blocks so an example row inside one is not
mistaken for a real one.
"""

from pathlib import Path
import re

import pytest

README = Path(__file__).resolve().parent.parent / "README.md"

ROW = re.compile(r"^\|(.+)\|\s*$")
SEPARATOR = re.compile(r"^\|[\s:|-]+\|\s*$")


def _table_problems() -> list[str]:
    lines = README.read_text(encoding="utf-8").splitlines()
    problems: list[str] = []
    expected: int | None = None
    in_table = False
    in_fence = False

    for number, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line.startswith("|"):
            expected = None
            in_table = False
            continue
        if SEPARATOR.match(line):
            continue
        match = ROW.match(line)
        if match is None:
            problems.append(f"line {number}: not a well-formed row: {line[:60]!r}")
            continue
        columns = len(match.group(1).split("|"))
        if not in_table or expected is None:
            expected = columns
            in_table = True
            continue
        if columns != expected:
            problems.append(
                f"line {number}: {columns} columns where the table has {expected}"
            )
    return problems


def test_every_readme_table_row_is_well_formed() -> None:
    """The real README, not a sample: every row matches its table's width."""
    problems = _table_problems()
    assert not problems, "\n".join(problems)


def test_the_check_itself_notices_a_merged_row() -> None:
    """Otherwise the test above is a function that always returns no problems.

    Verified by feeding it the exact defect it was written for: a row with an
    extra column, spliced into a real table.
    """
    broken = (
        "| Entity | Type | Notes |\n"
        "|---|---|---|\n"
        "| Status | sensor | Current state. |\n"
        "| Alignment | sensor | How the last one went. | Half of a row that got merged. |\n"
    )
    sample = (
        "| Entity | Type | Notes |\n"
        "|---|---|---|\n"
        "| Status | sensor | Current state. |\n"
        "| Alignment | sensor | How the last one went. |\n"
    )
    # Three counts each, because the separator row is not counted.
    assert _columns(broken) == [3, 3, 4]
    assert _columns(sample) == [3, 3, 3]


def _columns(text: str) -> list[int]:
    """Column counts per line, using the same rules as the check."""
    out: list[int] = []
    for line in text.splitlines():
        if SEPARATOR.match(line):
            continue
        match = ROW.match(line)
        out.append(len(match.group(1).split("|")) if match else 0)
    return out


def test_the_readme_documents_the_entities_that_exist() -> None:
    """A heading for each new group, so the table is not quietly behind.

    Asserted on the names rather than on a count, because a count drifts and a
    missing row does not announce itself.
    """
    text = README.read_text(encoding="utf-8")
    for name in (
        "Printhead alignment in progress",
        "Colours with a live alert",
        "Ink drops not recognised as HP",
        "Why the last update failed",
        "Wi-Fi encryption in use",
        "HTTP proxy configured",
        "Paper loaded",
        "Pages printed via the cloud",
    ):
        assert name in text, f"the entity table has no row for {name!r}"


@pytest.mark.parametrize("marker", ["```", "    "])
def test_fenced_content_is_not_counted(marker: str) -> None:
    """A pipe inside a code fence is not a table row."""
    sample = f"{marker}not | a | table {marker}\n"
    assert _columns(sample) == [0]
