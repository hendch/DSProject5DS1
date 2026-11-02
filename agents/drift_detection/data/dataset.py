"""Download, clean and split the UCI Bank Marketing dataset."""

from __future__ import annotations

import io
import ssl
import urllib.request
import zipfile

import numpy as np
import pandas as pd

from agents.drift_detection.config import Settings

SPLITS = ("train", "reference", "production")


def download(settings: Settings, force: bool = False) -> None:
    """Fetch the dataset CSV (a zip nested inside a zip, see config `data`) into data/."""
    cfg = settings["data"]
    target = settings.paths.raw_csv
    if target.exists() and not force:
        return
    with urllib.request.urlopen(cfg["source_url"], timeout=cfg["download_timeout_seconds"],
                                context=_ssl_context()) as resp:
        outer = zipfile.ZipFile(io.BytesIO(resp.read()))
    inner = zipfile.ZipFile(io.BytesIO(outer.read(cfg["archive_member"])))
    target.write_bytes(inner.read(cfg["csv_member"]))


def _ssl_context() -> ssl.SSLContext:
    """Verify HTTPS against the operating system's certificate store when available."""
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return ssl.create_default_context()


def load_raw(settings: Settings) -> pd.DataFrame:
    cfg = settings["data"]
    df = pd.read_csv(settings.paths.raw_csv, sep=cfg["csv_separator"])
    df = df.drop(columns=cfg["drop_columns"])
    df[settings.target] = (df[settings.target] == cfg["positive_label"]).astype(int)
    for col in settings.categorical:
        df[col] = df[col].astype("object")
    for col in settings.numeric:
        df[col] = df[col].astype(float)
    return df[settings.features + [settings.target]]


def make_splits(settings: Settings) -> dict[str, pd.DataFrame]:
    """Shuffle once, then cut into train / reference / production pool."""
    df = load_raw(settings)
    cfg = settings["data"]
    rng = np.random.default_rng(cfg["seed"])
    df = df.iloc[rng.permutation(len(df))].reset_index(drop=True)
    n_train = int(len(df) * cfg["train_fraction"])
    n_ref = int(len(df) * cfg["reference_fraction"])
    parts = {
        "train": df.iloc[:n_train],
        "reference": df.iloc[n_train : n_train + n_ref],
        "production": df.iloc[n_train + n_ref :],
    }
    for name, part in parts.items():
        part.reset_index(drop=True).to_csv(settings.paths.splits / f"{name}.csv", index=False)
    return parts


def load_split(settings: Settings, name: str) -> pd.DataFrame:
    if name not in SPLITS:
        raise ValueError(f"unknown split {name!r}")
    path = settings.paths.splits / f"{name}.csv"
    if not path.exists():
        raise FileNotFoundError("Dataset splits missing: run `drift-agent init` first.")
    return read_frame(settings, path)


def read_frame(settings: Settings, path) -> pd.DataFrame:
    """Read a CSV written by this app, restoring column types."""
    df = pd.read_csv(path, dtype={c: "object" for c in settings.categorical})
    for col in settings.numeric:
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df
