"""
lookup_gene_info Lambda tool
Queries amr_profiles for gene occurrence + joins static CARD/ResFinder gene reference data.
Returns JSON with gene_id, tool_name, confidence fields for grounding citations.
"""
import json
import logging
import os
import re
import time
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "amr_db")
ATHENA_CATALOG = os.environ.get("ATHENA_CATALOG", "AwsDataCatalog")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "primary")
ATHENA_RESULTS_BUCKET = os.environ.get("ATHENA_RESULTS_BUCKET", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")

athena = boto3.client("athena", region_name=REGION)

# Fixed statement. Parameters, in order: gene_id LIKE, organism LIKE. The table
# is unqualified; catalog and database come from QueryExecutionContext. The
# optional organism filter defaults to '%' (match all) to keep the statement a
# constant literal. amr_profiles schema: isolate_id, gene_id, gene_name,
# detection_tool, confidence, organism, run_id, ts.
SQL_LOOKUP_GENE_INFO = """
SELECT
    gene_id,
    detection_tool,
    organism,
    COUNT(*) AS detection_count,
    CAST(AVG(CAST(confidence AS DOUBLE)) AS VARCHAR) AS avg_confidence,
    CAST(MAX(CAST(confidence AS DOUBLE)) AS VARCHAR) AS max_confidence,
    CAST(MAX(ts) AS VARCHAR) AS latest_detection
FROM amr_profiles
WHERE LOWER(gene_id) LIKE LOWER(?)
  AND LOWER(organism) LIKE LOWER(?)
GROUP BY gene_id, detection_tool, organism
ORDER BY detection_count DESC
LIMIT 20
"""

# Static reference data for common AMR genes (from CARD/NCBI)
GENE_REFERENCE = {
    "blaTEM": {
        "full_name": "TEM beta-lactamase",
        "mechanism": "Antibiotic inactivation",
        "drug_class": "Beta-lactam",
        "source_db": "CARD",
        "aro_accession": "ARO:3000078",
    },
    "blaCTX-M": {
        "full_name": "CTX-M extended-spectrum beta-lactamase",
        "mechanism": "Antibiotic inactivation",
        "drug_class": "Cephalosporin",
        "source_db": "CARD",
        "aro_accession": "ARO:3000025",
    },
    "mcr": {
        "full_name": "Mobilized colistin resistance gene",
        "mechanism": "Target alteration",
        "drug_class": "Polymyxin",
        "source_db": "CARD",
        "aro_accession": "ARO:3003896",
    },
    "aac": {
        "full_name": "Aminoglycoside acetyltransferase",
        "mechanism": "Antibiotic inactivation",
        "drug_class": "Aminoglycoside",
        "source_db": "CARD",
        "aro_accession": "ARO:3003964",
    },
    "tetM": {
        "full_name": "Tetracycline resistance ribosomal protection protein TetM",
        "mechanism": "Target protection",
        "drug_class": "Tetracycline",
        "source_db": "CARD",
        "aro_accession": "ARO:3000189",
    },
    "sul": {
        "full_name": "Dihydropteroate synthase Sul",
        "mechanism": "Target replacement",
        "drug_class": "Sulfonamide",
        "source_db": "CARD",
        "aro_accession": "ARO:3000410",
    },
    "qnr": {
        "full_name": "Quinolone resistance pentapeptide repeat protein Qnr",
        "mechanism": "Target protection",
        "drug_class": "Fluoroquinolone",
        "source_db": "CARD",
        "aro_accession": "ARO:3001531",
    },
    "vanA": {
        "full_name": "D-Ala-D-Lac ligase VanA",
        "mechanism": "Target alteration",
        "drug_class": "Glycopeptide",
        "source_db": "CARD",
        "aro_accession": "ARO:3000601",
    },
    "mecA": {
        "full_name": "Penicillin-binding protein 2a (mecA)",
        "mechanism": "Target alteration",
        "drug_class": "Methicillin",
        "source_db": "CARD",
        "aro_accession": "ARO:3001155",
    },
}

# Allowlist pattern — gene IDs and organism names in AMR databases only contain
# alphanumerics, hyphens, underscores, dots, and spaces.
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z0-9\s\-_.]+$")


def _validate_ident(value: str, field: str) -> str:
    """Validate that a user-supplied identifier is safe to embed in a LIKE clause."""
    if not value:
        return value
    if not _SAFE_IDENT_RE.match(value):
        raise ValueError(f"Invalid characters in {field}: {value!r}")
    if len(value) > 100:
        raise ValueError(f"{field} too long (max 100 chars)")
    return value


def _get_reference_entry(gene_id: str) -> dict:
    """Fuzzy match gene_id against reference dict."""
    gene_upper = gene_id.upper()
    for key, val in GENE_REFERENCE.items():
        if key.upper() in gene_upper or gene_upper.startswith(key.upper()):
            return val
    return {}


def _run_query(sql: str, params: list[str] | None = None, max_results: int = 50) -> list[dict]:
    """Execute an Athena query. User values are passed as execution parameters
    (``?`` placeholders), never interpolated into the SQL string."""
    if not ATHENA_RESULTS_BUCKET:
        raise ValueError("ATHENA_RESULTS_BUCKET env var is not set")

    request = {
        "QueryString": sql,
        "QueryExecutionContext": {"Database": ATHENA_DATABASE, "Catalog": ATHENA_CATALOG},
        "WorkGroup": ATHENA_WORKGROUP,
        "ResultConfiguration": {"OutputLocation": f"s3://{ATHENA_RESULTS_BUCKET}/tool-queries/"},
    }
    if params:
        request["ExecutionParameters"] = params
    response = athena.start_query_execution(**request)
    exec_id = response["QueryExecutionId"]

    for _ in range(30):
        status = athena.get_query_execution(QueryExecutionId=exec_id)
        state = status["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status["QueryExecution"]["Status"].get("StateChangeReason", "Unknown")
            raise RuntimeError(f"Athena query {state}: {reason}")
        time.sleep(1)
    else:
        raise TimeoutError("Athena query timed out after 30s")

    results = athena.get_query_results(QueryExecutionId=exec_id, MaxResults=max_results + 1)
    rows = results["ResultSet"]["Rows"]
    if len(rows) <= 1:
        return []

    headers = [c["VarCharValue"] for c in rows[0]["Data"]]
    return [
        {headers[i]: col.get("VarCharValue", "") for i, col in enumerate(row["Data"])}
        for row in rows[1:]
    ]


def handler(event, context):
    """
    Input: { gene_id: str, organism?: str }
    Output: { gene_info: {...}, occurrences: [...], tool_name, gene_id, confidence }
    """
    try:
        body = event if isinstance(event, dict) else json.loads(event.get("body", "{}"))
        gene_id = _validate_ident(body.get("gene_id", "").strip(), "gene_id")
        organism = _validate_ident(body.get("organism", "").strip(), "organism")

        if not gene_id:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "gene_id parameter required", "tool_name": "lookup_gene_info"}),
            }

        # Fixed statement (SQL_LOOKUP_GENE_INFO). gene_id is bound as a LIKE
        # pattern; the optional organism filter defaults to '%' (match all) so the
        # statement stays constant. Parameter order matches the ? placeholders.
        params = [f"%{gene_id}%", f"%{organism}%" if organism else "%"]

        rows = _run_query(SQL_LOOKUP_GENE_INFO, params=params, max_results=20)

        # Get static reference entry
        ref_entry = _get_reference_entry(gene_id)

        occurrences = []
        for row in rows:
            occurrences.append({
                "gene_id": row.get("gene_id", gene_id),
                "tool_name": row.get("detection_tool", "amrfinderplus"),
                "confidence": row.get("avg_confidence", "0"),
                "organism": row.get("organism", ""),
                "detection_count": row.get("detection_count", "0"),
                "max_confidence": row.get("max_confidence", "0"),
                "latest_detection": row.get("latest_detection", ""),
            })

        # Overall confidence = max across all occurrences
        top_conf = max(
            (float(o["confidence"]) for o in occurrences if o["confidence"]),
            default=0.0,
        )

        logger.info(
            json.dumps({
                "level": "INFO",
                "tool": "lookup_gene_info",
                "gene_id": gene_id,
                "has_reference": bool(ref_entry),
                "occurrence_count": len(occurrences),
            })
        )

        return {
            "statusCode": 200,
            "body": json.dumps({
                "gene_info": {
                    "gene_id": gene_id,
                    **ref_entry,
                },
                "occurrences": occurrences,
                "tool_name": "lookup_gene_info",
                "gene_id": gene_id,
                "confidence": str(round(top_conf, 4)),
                "count": len(occurrences),
            }),
        }

    except Exception as exc:
        logger.error(json.dumps({"level": "ERROR", "tool": "lookup_gene_info", "error": str(exc)}))
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(exc), "tool_name": "lookup_gene_info", "occurrences": []}),
        }
