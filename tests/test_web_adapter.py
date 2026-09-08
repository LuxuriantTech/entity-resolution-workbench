from __future__ import annotations

from typing import Any, cast

import pytest
from conftest import module


def test_adapter_calls_real_resolve_split_and_exposes_engine_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = module("web_adapter")
    matcher = module("matcher")
    called: list[tuple[list[dict[str, str]], list[dict[str, str]], Any]] = []
    original = matcher.resolve_split

    def traced(left: list[dict[str, str]], right: list[dict[str, str]], config: Any) -> Any:
        called.append((left, right, config))
        return original(left, right, config)

    monkeypatch.setattr(matcher, "resolve_split", traced)
    results = adapter.resolve_catalogues(
        [
            {
                "source_id": "left-1",
                "name": "Cafetiere Eclair",
                "brand": "Acme",
                "sku": "X-1",
                "category": "kitchen",
                "price": "19.00",
            }
        ],
        [
            {
                "source_id": "right-1",
                "name": "Cafetiere Éclair",
                "brand": "Acme",
                "sku": "X-1",
                "category": "kitchen",
                "price": "19.00",
            }
        ],
    )
    assert len(called) == 1
    assert results[0]["decision"] == "MATCH"
    assert results[0]["scores"]["name"]["fraction"]
    assert results[0]["left"]["normalized"]["name"] == "cafetiere eclair"


def test_export_neutralizes_formula_cells_and_only_current_pair_values() -> None:
    adapter = module("web_adapter")
    payload = adapter.export_csv(
        [
            {
                "pair_id": "p",
                "decision": "REVIEW",
                "human_review": "UNREVIEWED",
                "left": {"raw": {"source_id": "=cmd"}},
                "right": {"raw": {"source_id": "+danger"}},
            }
        ]
    ).decode("utf-8")
    assert "'=cmd" in payload and "'+danger" in payload
    assert "truth" not in payload.casefold()


@pytest.mark.parametrize(
    "value",
    [
        "\u2003=SUM(A1:A2)",
        "\u00a0+CMD",
        "\v@CMD",
        "\f-2+3",
        "\tplain-tab-cell",
        "\rplain-carriage-return-cell",
    ],
)
def test_export_neutralizes_spreadsheet_control_prefixes_and_formula_markers(
    value: str,
) -> None:
    adapter = module("web_adapter")
    payload = adapter.export_csv(
        [
            {
                "pair_id": "p",
                "decision": "REVIEW",
                "human_review": "UNREVIEWED",
                "left": {"raw": {"source_id": value}},
                "right": {"raw": {"source_id": "safe"}},
            }
        ]
    ).decode("utf-8")
    assert "'" + value in payload


def test_adapter_normalizes_each_input_record_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = module("web_adapter")
    calls: list[str] = []
    original = adapter._record_payload

    def traced(row: dict[str, str]) -> dict[str, dict[str, str | None]]:
        calls.append(row["source_id"])
        return cast(dict[str, dict[str, str | None]], original(row))

    monkeypatch.setattr(adapter, "_record_payload", traced)
    left = [
        {
            "source_id": f"L{i}",
            "name": "Desk lamp",
            "brand": "Harbor",
            "sku": f"S{i}",
            "category": "lighting",
            "price": "20",
        }
        for i in range(2)
    ]
    right = [
        {
            "source_id": f"R{i}",
            "name": "Desk lamp",
            "brand": "Harbor",
            "sku": f"T{i}",
            "category": "lighting",
            "price": "20",
        }
        for i in range(2)
    ]

    adapter.resolve_catalogues(left, right)

    assert calls == ["L0", "L1", "R0", "R1"]


def test_adapter_rejects_pathological_similarity_work_before_calling_matcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = module("web_adapter")
    called = False

    def forbidden(*_args: object, **_kwargs: object) -> list[object]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(adapter.matcher, "resolve_split", forbidden)
    hostile = "a" * 4_096
    row = {
        "source_id": "L1",
        "name": hostile,
        "brand": "",
        "sku": "",
        "category": "",
        "price": "",
    }
    other = {**row, "source_id": "R1", "name": hostile[:-1] + "b"}

    with pytest.raises(adapter.ResourceBoundError, match="similarity work"):
        adapter.resolve_catalogues([row], [other])

    assert called is False


def test_adapter_allows_one_hundred_short_records_per_side_within_work_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = module("web_adapter")
    calls = 0

    def traced(*_args: object, **_kwargs: object) -> list[object]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(adapter.matcher, "resolve_split", traced)
    left = [
        {
            "source_id": f"L{index}",
            "name": "Synthetic desk lamp",
            "brand": "Harbor",
            "sku": "",
            "category": "",
            "price": "",
        }
        for index in range(100)
    ]
    right = [
        {
            "source_id": f"R{index}",
            "name": "Synthetic desk lamp",
            "brand": "Harbor",
            "sku": "",
            "category": "",
            "price": "",
        }
        for index in range(100)
    ]

    assert adapter.resolve_catalogues(left, right) == []
    assert calls == 1


def test_export_fails_closed_before_exceeding_its_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = module("web_adapter")
    monkeypatch.setattr(adapter, "MAX_EXPORT_BYTES", 64)
    with pytest.raises(module("common").ResourceBoundError):
        adapter.export_csv(
            [
                {
                    "pair_id": "p",
                    "decision": "REVIEW",
                    "human_review": "UNREVIEWED",
                    "left": {"raw": {"source_id": "safe"}},
                    "right": {"raw": {"source_id": "safe"}},
                }
            ]
        )


def test_pair_ids_are_unambiguous_and_source_ids_remain_visible() -> None:
    adapter = module("web_adapter")
    rows_left = [
        {
            "source_id": "a:b",
            "name": "Lamp",
            "brand": "A",
            "sku": "1",
            "category": "x",
            "price": "1",
        },
        {
            "source_id": "a",
            "name": "Shelf",
            "brand": "B",
            "sku": "2",
            "category": "y",
            "price": "2",
        },
    ]
    rows_right = [
        {"source_id": "c", "name": "Lamp", "brand": "A", "sku": "1", "category": "x", "price": "1"},
        {
            "source_id": "b:c",
            "name": "Shelf",
            "brand": "B",
            "sku": "2",
            "category": "y",
            "price": "2",
        },
    ]

    pairs = adapter.resolve_catalogues(rows_left, rows_right)

    assert len({pair["pair_id"] for pair in pairs}) == len(pairs)
    assert {(pair["left_id"], pair["right_id"]) for pair in pairs} == {
        (left["source_id"], right["source_id"]) for left in rows_left for right in rows_right
    }
