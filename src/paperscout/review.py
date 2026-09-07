from __future__ import annotations

import re

from .models import ReviewDecision, ReviewVerdict


VERDICT_LINE = re.compile(r"^VERDICT: (APPROVE|REVISE|REJECT)$")


class ReviewParseError(ValueError):
    pass


def parse_review_response(response: str) -> ReviewDecision:
    """Parse only the first line and fail closed on every unknown form."""
    lines = response.splitlines()
    first_line = lines[0].strip() if lines else ""
    match = VERDICT_LINE.fullmatch(first_line)
    if match is None:
        raise ReviewParseError("Review response first line must be VERDICT: APPROVE, REVISE, or REJECT")
    feedback = [line.strip() for line in lines[1:] if line.strip()]
    return ReviewDecision(verdict=ReviewVerdict(match.group(1)), feedback=feedback)
