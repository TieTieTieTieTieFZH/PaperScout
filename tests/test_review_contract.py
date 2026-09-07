from __future__ import annotations

import pytest

from paperscout.models import ReviewVerdict
from paperscout.review import ReviewParseError, parse_review_response


def test_review_parser_accepts_strict_first_line_and_collects_feedback() -> None:
    decision = parse_review_response("VERDICT: REVISE\n\n1. 缩小结论范围。\n2. 补充实验依据。")

    assert decision.verdict == ReviewVerdict.REVISE
    assert decision.feedback == ["1. 缩小结论范围。", "2. 补充实验依据。"]


@pytest.mark.parametrize(
    "response",
    [
        "\nVERDICT: APPROVE\n未发现问题。",
        "APPROVE\n未发现问题。",
        "VERDICT:APPROVE\n未发现问题。",
        "VERDICT: UNKNOWN\n未发现问题。",
        "说明：通过\nVERDICT: APPROVE",
        "",
    ],
)
def test_review_parser_fails_closed_when_first_line_is_invalid(response: str) -> None:
    with pytest.raises(ReviewParseError):
        parse_review_response(response)
