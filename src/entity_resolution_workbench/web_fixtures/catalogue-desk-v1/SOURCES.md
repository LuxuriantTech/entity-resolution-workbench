# Fixture provenance

`left.csv` and `right.csv` are project-authored synthetic product catalogues created on
2026-09-04 for the local review journey. They contain no copied catalogue, customer, person,
account, or production data.

The records deliberately exercise a clear SKU-supported match, a typo and accent normalization,
a missing price, a repeated product name, a conflicting SKU, an ambiguous review, and unrelated
products. There is no truth file in this fixture. Expected decisions are intentionally absent from
the fixture and are computed only by the real matcher at runtime.
