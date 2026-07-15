"""Deterministic fingerprints for point-in-time research panels."""

from __future__ import annotations

import hashlib
import json

import pandas as pd


def dataframe_fingerprint(df: pd.DataFrame) -> str:
    """Return an order-invariant hash of columns, dtypes, and row contents."""
    columns = sorted(str(column) for column in df.columns)
    canonical = df.loc[:, columns]
    row_hashes = pd.util.hash_pandas_object(
        canonical,
        index=False,
        categorize=True,
    ).to_numpy(dtype="uint64")
    row_hashes.sort()
    metadata = {
        "columns": columns,
        "dtypes": [str(canonical[column].dtype) for column in columns],
        "rows": len(canonical),
    }
    digest = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode("utf-8"))
    digest.update(row_hashes.tobytes())
    return digest.hexdigest()
