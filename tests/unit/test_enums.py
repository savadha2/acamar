"""The controlled vocabularies behave as the DB layer assumes."""

from __future__ import annotations

import re
from enum import StrEnum

import pytest

from acamar.domain.enums import Category, Domain, Organization, SourceStatus

_SNAKE = re.compile(r"^[a-z]+(_[a-z]+)*$")


@pytest.mark.parametrize("enum_cls", [Domain, Category, SourceStatus, Organization])
def test_values_are_lowercase_snake_case(enum_cls: type[StrEnum]) -> None:
    for member in enum_cls:
        assert _SNAKE.match(member.value), member


def test_str_enum_renders_as_its_value() -> None:
    # the DB layer relies on a member being usable as its plain string value
    assert Domain.SCHOOL.value == "school"
    assert str(Category.EVENT) == "event"
    assert f"{Category.MENU}" == "menu"
    assert Domain("school") is Domain.SCHOOL
