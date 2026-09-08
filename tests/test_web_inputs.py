from __future__ import annotations

import base64
from collections.abc import Iterator

import pytest
from conftest import module


def test_upload_preview_neutralizes_filename_and_parses_bounded_csv() -> None:
    inputs = module("web_inputs")
    payload = b"product,make,code,amount\nCafetiere,Acme,SKU-1,19.00\n"
    preview = inputs.parse_upload(
        filename=r"C:\\fakepath\\<bidi\u202efile>.csv",
        content_base64=base64.b64encode(payload).decode("ascii"),
    )
    assert preview.display_name == "file.csv"
    assert preview.headers == ("product", "make", "code", "amount")
    assert preview.row_count == 1


@pytest.mark.parametrize("encoded", ["%%", base64.b64encode(b"a\0b").decode("ascii")])
def test_upload_preview_rejects_invalid_or_nul_encoded_bytes(encoded: str) -> None:
    with pytest.raises(module("web_inputs").WebInputError):
        module("web_inputs").parse_upload(filename="input.csv", content_base64=encoded)


def test_mapping_requires_unique_source_id_and_descriptive_field() -> None:
    inputs = module("web_inputs")
    headers = ("id", "title", "brand", "sku", "price")
    with pytest.raises(module("web_inputs").WebInputError):
        inputs.validate_mapping(
            {
                "source_id": "id",
                "name": "title",
                "brand": "title",
                "sku": None,
                "category": None,
                "price": None,
            },
            headers,
        )


def test_upload_accepts_bounded_extra_columns_for_explicit_mapping() -> None:
    inputs = module("web_inputs")
    payload = (
        b"id,title,brand,sku,category,price,ignored\n1,Desk lamp,Harbor,H-1,Lighting,20,note\n"
    )
    preview = inputs.parse_upload(
        filename="catalogue.csv",
        content_base64=base64.b64encode(payload).decode("ascii"),
    )
    assert preview.headers[-1] == "ignored"
    assert preview.row_count == 1


def test_upload_rejects_malformed_quoted_csv() -> None:
    inputs = module("web_inputs")
    payload = b'id,title\n1,"unterminated\n'
    with pytest.raises(inputs.WebInputError):
        inputs.parse_upload(
            filename="catalogue.csv",
            content_base64=base64.b64encode(payload).decode("ascii"),
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"id,,name\n1,x,Lamp\n",
        b"id,id,name\n1,2,Lamp\n",
        b"id,name\n1,Lamp,unexpected\n",
        (",".join(f"column_{index}" for index in range(33)) + "\n").encode(),
        ("id,name\n" + "1," + "x" * 4_097 + "\n").encode(),
        ("id,name\n" + "\n".join(f"{index},Lamp" for index in range(101)) + "\n").encode(),
    ],
)
def test_upload_rejects_empty_and_resource_hostile_csv_structures(payload: bytes) -> None:
    inputs = module("web_inputs")
    with pytest.raises((inputs.WebInputError, module("common").ResourceBoundError)):
        inputs.parse_upload(
            filename="catalogue.csv",
            content_base64=base64.b64encode(payload).decode("ascii"),
        )


def test_upload_accepts_bom_and_quoted_commas_newlines_without_formula_execution() -> None:
    inputs = module("web_inputs")
    payload = b'\xef\xbb\xbfid,name\n1,"Lamp, with\nline"\n2,=SUM(A1:A2)\n'
    preview = inputs.parse_upload(
        filename="quoted.csv",
        content_base64=base64.b64encode(payload).decode("ascii"),
    )
    assert preview.headers == ("id", "name")
    assert preview.rows[0]["name"] == "Lamp, with\nline"
    assert preview.rows[1]["name"] == "=SUM(A1:A2)"


def test_upload_rejects_decoded_bytes_above_the_frozen_file_bound() -> None:
    inputs = module("web_inputs")
    maximum = module("scoring").default_config().max_csv_file_bytes
    with pytest.raises(module("common").ResourceBoundError):
        inputs.parse_upload(
            filename="large.csv",
            content_base64=base64.b64encode(b"x" * (maximum + 1)).decode("ascii"),
        )


def test_upload_stops_reading_at_the_first_row_beyond_the_frozen_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = module("web_inputs")

    class CountingReader:
        fieldnames = ("id", "name")

        def __init__(self) -> None:
            self.yielded = 0

        def __iter__(self) -> Iterator[dict[str, str]]:
            for index in range(1_000):
                self.yielded += 1
                yield {"id": f"L-{index}", "name": "Synthetic lamp"}

    reader = CountingReader()
    monkeypatch.setattr(inputs.csv, "DictReader", lambda *_args, **_kwargs: reader)

    with pytest.raises(inputs.ResourceBoundError):
        inputs.parse_upload(
            filename="bounded.csv",
            content_base64=base64.b64encode(b"id,name\n").decode("ascii"),
        )

    assert reader.yielded == inputs.default_config().max_catalog_records + 1
