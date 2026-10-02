"""
concordance-computer Lambda
FR-016: Reads ast_phenotypes Iceberg table via Athena, computes per-drug-class
concordance vs. amr_profiles, writes back to Iceberg.
If no AST data is available, skips without error.
"""
import json
import os
import re
import time
from datetime import datetime, timezone

import boto3

ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "")
ATHENA_RESULTS_BUCKET = os.environ.get("ATHENA_RESULTS_BUCKET", "")
# GLUE_DATABASE holds the federated S3 Tables catalog name ({prefix}_amr_db).
# The Iceberg database inside that catalog is always "amr_db". Both are supplied
# through Athena's QueryExecutionContext so table names stay unqualified in the
# SQL and every statement is a fixed literal.
GLUE_DATABASE = os.environ.get("GLUE_DATABASE", "")
ICEBERG_DATABASE = "amr_db"
REGION = os.environ.get("AWS_REGION", "us-west-2")

athena = boto3.client("athena", region_name=REGION)

# Safe identifier pattern for isolate_id (alphanumeric, dash, underscore)
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")

# Fixed statements. The tables are unqualified; the catalog and database come
# from QueryExecutionContext. Row values are bound as execution parameters.
SQL_AST_COUNT = """
SELECT COUNT(*) AS ast_count
FROM ast_phenotypes
WHERE isolate_id = ?
"""

# Parameters, in order: run_id (SELECT), isolate_id (WHERE).
SQL_CONCORDANCE = """
SELECT
    ast.isolate_id,
    ast.drug_class,
    ast.interpretation AS ast_interpretation,
    COUNT(DISTINCT amr.gene_id) AS predicted_resistance_genes,
    CASE
        WHEN ast.interpretation = 'R' AND COUNT(DISTINCT amr.gene_id) > 0 THEN 'CONCORDANT'
        WHEN ast.interpretation = 'S' AND COUNT(DISTINCT amr.gene_id) = 0 THEN 'CONCORDANT'
        ELSE 'DISCORDANT'
    END AS concordance,
    CURRENT_TIMESTAMP AS computed_ts,
    ? AS run_id
FROM ast_phenotypes ast
LEFT JOIN amr_profiles amr
    ON ast.isolate_id = amr.isolate_id
WHERE ast.isolate_id = ?
GROUP BY ast.isolate_id, ast.drug_class, ast.interpretation
"""


def log(level: str, msg: str, **kwargs) -> None:
    record = {
        "level": level,
        "message": msg,
        "run_id": kwargs.get("run_id", ""),
        "isolate_id": kwargs.get("isolate_id", ""),
        "stage": "concordance-computer",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    record.update(kwargs)
    print(json.dumps(record))


def sanitize_id(value: str) -> str:
    """Sanitize identifier to prevent SQL injection. Raises ValueError if unsafe."""
    if not SAFE_ID_RE.match(value):
        raise ValueError(f"Unsafe identifier rejected: {repr(value)}")
    return value


def run_athena_query(sql: str, workgroup: str, output_location: str,
                     params: list | None = None) -> list:
    """Execute Athena query and return rows as list of dicts.

    The catalog and database are supplied through QueryExecutionContext so table
    names stay unqualified in the SQL. Row values are passed as Athena execution
    parameters (``?`` placeholders), never interpolated into the SQL string.
    """
    request = {
        "QueryString": sql,
        "QueryExecutionContext": {"Database": ICEBERG_DATABASE, "Catalog": GLUE_DATABASE},
        "WorkGroup": workgroup,
        "ResultConfiguration": {"OutputLocation": output_location},
    }
    if params:
        request["ExecutionParameters"] = params
    resp = athena.start_query_execution(**request)
    exec_id = resp["QueryExecutionId"]

    # Poll until complete
    for _ in range(60):  # max 5 minutes
        time.sleep(5)
        status_resp = athena.get_query_execution(QueryExecutionId=exec_id)
        execution = status_resp.get("QueryExecution", {})
        state = execution.get("Status", {}).get("State", "UNKNOWN")
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = execution.get("Status", {}).get("StateChangeReason", "unknown reason")
            raise RuntimeError(f"Athena query {exec_id} {state}: {reason}")

    # Fetch results
    rows = []
    paginator = athena.get_paginator("get_query_results")
    pages = paginator.paginate(QueryExecutionId=exec_id)
    headers = None
    for page in pages:
        result_rows = page.get("ResultSet", {}).get("Rows", [])
        if not result_rows:
            continue
        if headers is None:
            headers = [col.get("VarCharValue", "") for col in result_rows[0].get("Data", [])]
            result_rows = result_rows[1:]  # skip header row
        for row in result_rows:
            values = [col.get("VarCharValue", "") for col in row.get("Data", [])]
            if headers:
                rows.append(dict(zip(headers, values)))
    return rows


def handler(event: dict, context) -> dict:
    """
    Expected event shape:
    {
        "isolate_id": "ISO-001",
        "run_id": "run-2024-01-01",
        ...
    }
    """
    isolate_id = event.get("isolate_id", "")
    run_id = event.get("run_id", "")

    log("info", "concordance-computer invoked", run_id=run_id, isolate_id=isolate_id)

    if not ATHENA_WORKGROUP or not GLUE_DATABASE:
        log("warn", "Athena config missing, skipping concordance", run_id=run_id, isolate_id=isolate_id)
        return {**event, "concordance_skipped": True, "concordance_reason": "config_missing"}

    # Validate isolate_id against a strict allowlist before use. It is also bound
    # as an execution parameter in every query, so it is never part of the SQL
    # text; the catalog and database are fixed server-side via QueryExecutionContext.
    try:
        sanitize_id(isolate_id)
    except ValueError as e:
        log("warn", f"Unsafe identifier rejected: {e}", run_id=run_id, isolate_id=isolate_id)
        return {**event, "concordance_skipped": True, "concordance_reason": "unsafe_identifier"}

    output_location = f"s3://{ATHENA_RESULTS_BUCKET}/concordance/"

    # Step 1: Check if AST phenotype data exists for this isolate
    try:
        check_rows = run_athena_query(SQL_AST_COUNT, ATHENA_WORKGROUP, output_location,
                                      params=[isolate_id])
    except Exception as e:
        log("warn", f"AST check query failed (non-fatal): {e}", run_id=run_id, isolate_id=isolate_id)
        return {**event, "concordance_skipped": True, "concordance_reason": "ast_query_failed"}

    ast_count = 0
    if check_rows:
        raw_count = check_rows[0].get("ast_count", "0")
        try:
            ast_count = int(raw_count)
        except (ValueError, TypeError):
            ast_count = 0

    if ast_count == 0:
        log("info", "No AST phenotype data found, skipping concordance",
            run_id=run_id, isolate_id=isolate_id)
        return {**event, "concordance_skipped": True, "concordance_reason": "no_ast_data"}

    # Step 2: Compute concordance — join amr_profiles with ast_phenotypes.
    # run_id and isolate_id are bound as execution parameters in placeholder
    # order (run_id in SELECT, then isolate_id in WHERE).
    try:
        concordance_rows = run_athena_query(SQL_CONCORDANCE, ATHENA_WORKGROUP, output_location,
                                            params=[run_id, isolate_id])
    except Exception as e:
        log("warn", f"Concordance computation failed (non-fatal): {e}", run_id=run_id, isolate_id=isolate_id)
        return {**event, "concordance_skipped": True, "concordance_reason": "concordance_query_failed"}

    concordance_summary = []
    for row in concordance_rows:
        concordance_summary.append({
            "drug_class": row.get("drug_class", ""),
            "concordance": row.get("concordance", ""),
            "ast_interpretation": row.get("ast_interpretation", ""),
            "predicted_resistance_genes": row.get("predicted_resistance_genes", "0"),
        })

    log("info", "Concordance computed",
        run_id=run_id, isolate_id=isolate_id,
        drug_classes=len(concordance_summary))

    return {
        **event,
        "concordance_skipped": False,
        "concordance_computed": True,
        "concordance_summary": concordance_summary,
    }
