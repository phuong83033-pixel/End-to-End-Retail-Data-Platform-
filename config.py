"""Central configuration for the Retail Sales Intelligence Platform.

This is the ONLY place where credentials and connection settings are read.
Every other module imports from here, so a credential never appears twice in
the codebase. All values come from the `.env` file at the project root
(see `.env.example` for the full list of keys).
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from minio import Minio

# Project root = the directory this file lives in.
PROJECT_ROOT = Path(__file__).resolve().parent

# Load `.env` from the project root regardless of the current working directory,
# so the pipeline behaves the same whether it is started from the root or from
# the Ingestion/ folder.
load_dotenv(PROJECT_ROOT / ".env")


# ---------------------------------------------------------------------------
# Source: Xom Data SQL Server (OLTP) - read-only
# ---------------------------------------------------------------------------
OLTP_HOST = os.getenv("OLTP_HOST")
OLTP_PORT = int(os.getenv("OLTP_PORT", "1433"))
OLTP_DATABASE = os.getenv("OLTP_DATABASE")
OLTP_USERNAME = os.getenv("OLTP_USERNAME")
OLTP_PASSWORD = os.getenv("OLTP_PASSWORD")
OLTP_SCHEMA = os.getenv("OLTP_SCHEMA", "retails")

# Logical name of the source system, stamped onto every ingested row.
SOURCE_SYSTEM = os.getenv("SOURCE_SYSTEM", "xom_data_sqlserver")


# ---------------------------------------------------------------------------
# Bronze layer: MinIO (S3-compatible object storage)
# Defaults intentionally match docker-compose.yaml so a fresh clone works.
# ---------------------------------------------------------------------------
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadminpassword")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").strip().lower() in ("1", "true", "yes")
MINIO_BRONZE_BUCKET = os.getenv("MINIO_BRONZE_BUCKET", "bronze")


# ---------------------------------------------------------------------------
# Silver / Gold layers: local DuckDB warehouse file (read by dbt-duckdb)
# ---------------------------------------------------------------------------
DUCKDB_PATH = os.getenv(
    "DUCKDB_PATH", str(PROJECT_ROOT / "duckdb_warehouse" / "warehouse.duckdb")
)


# Environment variables that have no safe default and must be supplied by the user.
_REQUIRED_SETTINGS = (
    "OLTP_HOST",
    "OLTP_DATABASE",
    "OLTP_USERNAME",
    "OLTP_PASSWORD",
)


def validate_config() -> None:
    """Raise a ValueError naming every required setting that is missing.

    Called at the start of the ingestion run so the pipeline fails fast with an
    actionable message instead of a confusing driver-level connection error.
    """
    missing = [name for name in _REQUIRED_SETTINGS if not globals().get(name)]
    if missing:
        raise ValueError(
            "Missing required configuration in .env: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill in the values."
        )


def get_minio_client() -> Minio:
    """Return a MinIO SDK client for the Bronze object store.

    Used for bucket administration (creating the bronze bucket); the actual
    Parquet writing is done by dlt through the S3 API.
    """
    return Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE,
    )


def get_minio_endpoint_url() -> str:
    """Return the MinIO endpoint as a full URL (e.g. http://localhost:9000).

    The MinIO SDK wants a bare `host:port`, while S3 clients (dlt, DuckDB
    httpfs) want a scheme-prefixed URL - this derives one from the other.
    """
    scheme = "https" if MINIO_SECURE else "http"
    return f"{scheme}://{MINIO_ENDPOINT}"


def get_dlt_filesystem_credentials() -> dict:
    """Return the S3 credential dict expected by dlt's filesystem destination."""
    return {
        "aws_access_key_id": MINIO_ACCESS_KEY,
        "aws_secret_access_key": MINIO_SECRET_KEY,
        "endpoint_url": get_minio_endpoint_url(),
    }


def get_bronze_bucket_url() -> str:
    """Return the Bronze layer bucket URL (e.g. s3://bronze)."""
    return f"s3://{MINIO_BRONZE_BUCKET}"


def get_sql_server_connection_kwargs() -> dict:
    """Return the keyword arguments for opening a pymssql connection."""
    return {
        "server": OLTP_HOST,
        "port": OLTP_PORT,
        "user": OLTP_USERNAME,
        "password": OLTP_PASSWORD,
        "database": OLTP_DATABASE,
    }
