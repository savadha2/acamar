"""ManualMenuConnector — reads whatever the Pilot Cockpit's upload endpoint staged."""

from __future__ import annotations

from pathlib import Path

import pytest

from acamar.ports.source_connector import ConnectorError
from acamar.sources.manual_menu.connector import ManualMenuConnector

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "manual_menu"


def _stage(upload_dir: Path, meal: str, year_month: str, fixture_name: str) -> None:
    meal_dir = upload_dir / meal
    meal_dir.mkdir(parents=True, exist_ok=True)
    (meal_dir / f"{year_month}.pdf").write_bytes((_FIXTURES / fixture_name).read_bytes())


async def test_no_uploads_yet_returns_no_documents(tmp_path: Path) -> None:
    connector = ManualMenuConnector(upload_dir=str(tmp_path))

    documents = await connector.fetch()

    assert documents == []


async def test_parses_a_staged_lunch_pdf(tmp_path: Path) -> None:
    _stage(tmp_path, "lunch", "2026-09", "lunch_2026-09.pdf")
    connector = ManualMenuConnector(upload_dir=str(tmp_path))

    documents = await connector.fetch()

    assert len(documents) == 20
    assert all(d.external_id.startswith("menu::lunch::2026-09-") for d in documents)


async def test_parses_both_meals_together(tmp_path: Path) -> None:
    _stage(tmp_path, "lunch", "2026-09", "lunch_2026-09.pdf")
    _stage(tmp_path, "breakfast", "2026-09", "breakfast_2026-09.pdf")
    connector = ManualMenuConnector(upload_dir=str(tmp_path))

    documents = await connector.fetch()

    assert sum(1 for d in documents if "lunch" in d.external_id) == 20
    assert sum(1 for d in documents if "breakfast" in d.external_id) == 20


async def test_a_prior_months_pdf_is_still_parsed_every_run(tmp_path: Path) -> None:
    # Not "latest only" — a past month is still a legitimate, queryable
    # document (see the connector's own docstring on why reconciliation is
    # skipped for this source).
    _stage(tmp_path, "lunch", "2026-08", "lunch_2026-09.pdf")  # reuse the fixture's shape
    _stage(tmp_path, "lunch", "2026-09", "lunch_2026-09.pdf")
    connector = ManualMenuConnector(upload_dir=str(tmp_path))

    documents = await connector.fetch()

    assert any(d.external_id.startswith("menu::lunch::2026-08-") for d in documents)
    assert any(d.external_id.startswith("menu::lunch::2026-09-") for d in documents)


async def test_a_file_not_matching_the_staged_naming_convention_is_ignored(
    tmp_path: Path,
) -> None:
    meal_dir = tmp_path / "lunch"
    meal_dir.mkdir(parents=True)
    (meal_dir / "not-a-staged-file.pdf").write_bytes(b"garbage")
    connector = ManualMenuConnector(upload_dir=str(tmp_path))

    documents = await connector.fetch()

    assert documents == []


async def test_a_corrupt_staged_file_raises_connector_error(tmp_path: Path) -> None:
    _stage(tmp_path, "lunch", "2026-09", "lunch_2026-09.pdf")
    (tmp_path / "lunch" / "2026-09.pdf").write_bytes(b"not actually a pdf")
    connector = ManualMenuConnector(upload_dir=str(tmp_path))

    with pytest.raises(ConnectorError, match="manual_menu"):
        await connector.fetch()
