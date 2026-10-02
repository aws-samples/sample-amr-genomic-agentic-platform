"""
ingestion-validator Lambda
FR-020 / NFR-014: Validates SRA accession against public NCBI sources only.
Records source_provenance and license in DynamoDB and isolate_metadata.
"""
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ALLOWED_SOURCES = set(os.environ.get("ALLOWED_SOURCES", "ncbi-sra-public,ncbi-trace-public").split(","))
DYNAMO_TABLE = os.environ.get("DYNAMO_TABLE_NAME", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")

# Organisms AMRFinderPlus accepts for its --organism flag (enables
# species-specific point-mutation screening). Passing a value outside this set
# makes amrfinder fail, so a requested organism that is not supported is kept as
# a free-text label for storage but the AMRFinder flag is left blank, which runs
# generic acquired-gene detection. Source of truth: `amrfinder --list_organisms`
# for the database baked into the funcscan image (4.2.7 / 2026-08-07.1).
AMRFINDER_ORGANISMS = {
    "Acinetobacter_baumannii", "Bordetella_pertussis", "Burkholderia_cepacia",
    "Burkholderia_mallei", "Burkholderia_pseudomallei", "Campylobacter",
    "Citrobacter_freundii", "Clostridioides_difficile", "Corynebacterium_diphtheriae",
    "Enterobacter_asburiae", "Enterobacter_cloacae", "Enterococcus_faecalis",
    "Enterococcus_faecium", "Escherichia", "Haemophilus_influenzae",
    "Helicobacter_pylori", "Klebsiella_oxytoca", "Klebsiella_pneumoniae",
    "Neisseria_gonorrhoeae", "Neisseria_meningitidis", "Pseudomonas_aeruginosa",
    "Salmonella", "Serratia_marcescens", "Staphylococcus_aureus",
    "Staphylococcus_epidermidis", "Staphylococcus_pseudintermedius",
    "Streptococcus_agalactiae", "Streptococcus_pneumoniae", "Streptococcus_pyogenes",
    "Vibrio_cholerae", "Vibrio_parahaemolyticus", "Vibrio_vulnificus",
}

# Common species names mapped to the AMRFinderPlus --organism token so callers
# can pass a familiar label (e.g. "E. coli") and still get species-aware screening.
_ORGANISM_ALIASES = {
    "e. coli": "Escherichia", "e.coli": "Escherichia", "escherichia coli": "Escherichia",
    "escherichia": "Escherichia",
    "salmonella": "Salmonella", "salmonella enterica": "Salmonella",
    "k. pneumoniae": "Klebsiella_pneumoniae", "klebsiella pneumoniae": "Klebsiella_pneumoniae",
    "s. aureus": "Staphylococcus_aureus", "staphylococcus aureus": "Staphylococcus_aureus",
    "p. aeruginosa": "Pseudomonas_aeruginosa", "pseudomonas aeruginosa": "Pseudomonas_aeruginosa",
    "campylobacter": "Campylobacter",
    "c. difficile": "Clostridioides_difficile", "clostridioides difficile": "Clostridioides_difficile",
    "listeria": "", "listeria monocytogenes": "",  # not supported by --organism; store label, run generic
}


def resolve_organism(raw: str):
    """Map a requested organism to a storage label and an AMRFinder flag value.

    Returns (organism_label, amrfinder_organism). organism_label is the human
    label stored with the isolate; amrfinder_organism is the exact AMRFinderPlus
    token or '' when the organism is not supported (generic detection).
    """
    label = (raw or "Salmonella").strip()
    key = label.lower()
    if label in AMRFINDER_ORGANISMS:
        return label, label
    if key in _ORGANISM_ALIASES:
        mapped = _ORGANISM_ALIASES[key]
        # Keep the caller's label for display; use the mapped token for the flag.
        return label, mapped
    # Unknown organism: store the label, run generic detection.
    return label, ""

dynamo = boto3.client("dynamodb", region_name=REGION)

# SRA public accession patterns: SRR, ERR, DRR prefixes (public)
PUBLIC_ACCESSION_RE = re.compile(r"^(SRR|ERR|DRR|SRX|SRS|SRP|ERP|DRP)\d+$", re.IGNORECASE)
# Controlled-access patterns: dbGaP-protected projects
CONTROLLED_PATTERNS = [re.compile(p) for p in [
    r"^phs\d+",        # dbGaP study
    r"_controlled",    # explicit controlled marker
    r"_dbgap",         # dbGaP marker
]]


def log(level: str, msg: str, **kwargs) -> None:
    record = {
        "level": level,
        "message": msg,
        "run_id": kwargs.get("run_id", ""),
        "isolate_id": kwargs.get("isolate_id", ""),
        "stage": "ingestion-validator",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    record.update(kwargs)
    print(json.dumps(record))


def is_controlled_access(accession: str, source: str) -> bool:
    """Return True if this accession or source indicates controlled access."""
    acc_lower = accession.lower()
    src_lower = source.lower()
    for pattern in CONTROLLED_PATTERNS:
        if pattern.search(acc_lower) or pattern.search(src_lower):
            return True
    return False


def resolve_source_and_license(accession: str, source: str):
    """
    Determine source_provenance and license from accession and source hint.
    For public NCBI SRA data, all WGS data is US Government public domain.
    """
    source_map = {
        "ncbi-sra-public": {
            "source_provenance": "NCBI SRA Public",
            "license": "US Government public domain (NCBI SRA)",
        },
        "ncbi-trace-public": {
            "source_provenance": "NCBI Trace Archive Public",
            "license": "US Government public domain (NCBI Trace)",
        },
    }
    src_key = source.lower().strip()
    if src_key in source_map:
        return source_map[src_key]
    # Default for any other public NCBI source
    return {
        "source_provenance": f"NCBI SRA Public ({source})",
        "license": "US Government public domain (NCBI SRA)",
    }


def record_to_dynamo(isolate_id: str, accession: str, source_provenance: str, license_str: str, run_id: str) -> None:
    """Write provenance record to DynamoDB. Non-fatal on failure."""
    if not DYNAMO_TABLE:
        log("warn", "DYNAMO_TABLE_NAME not set, skipping DynamoDB write", run_id=run_id, isolate_id=isolate_id)
        return
    try:
        event_ts = datetime.now(timezone.utc).isoformat()
        dynamo.put_item(
            TableName=DYNAMO_TABLE,
            Item={
                "isolate_id":        {"S": isolate_id},
                "event_ts":          {"S": event_ts},
                "run_id":            {"S": run_id},
                "stage":             {"S": "ingestion-validator"},
                "accession":         {"S": accession},
                "source_provenance": {"S": source_provenance},
                "license":           {"S": license_str},
                "status":            {"S": "VALIDATED"},
            },
        )
        log("info", "DynamoDB provenance record written", run_id=run_id, isolate_id=isolate_id)
    except Exception as e:
        log("error", f"DynamoDB write failed (non-fatal): {e}", run_id=run_id, isolate_id=isolate_id)


def handler(event: dict, context) -> dict:
    """
    Expected event shape (single isolate):
    {
        "isolate_id": "ISO-001",
        "accession": "SRR123456",
        "source": "ncbi-sra-public",   # optional, defaults to ncbi-sra-public
        "run_id": "run-2024-01-01"
    }
    Returns enriched dict with source_provenance and license, or raises ValueError.
    """
    isolate_id = event.get("isolate_id", str(uuid.uuid4()))
    accession = event.get("accession", "").strip()
    source = event.get("source", "ncbi-sra-public").strip()
    run_id = event.get("run_id", "")
    organism_label, amrfinder_organism = resolve_organism(event.get("organism", "Salmonella"))

    log("info", "ingestion-validator invoked", run_id=run_id, isolate_id=isolate_id,
        accession=accession, source=source, organism=organism_label,
        amrfinder_organism=amrfinder_organism or "(generic)")

    # --- Validation: controlled access check ---
    if is_controlled_access(accession, source):
        log("warn", "REJECTED: controlled-access source detected",
            run_id=run_id, isolate_id=isolate_id, accession=accession, source=source)
        raise ValueError(
            f"Controlled-access source rejected: accession={accession} source={source}. "
            "Only public NCBI SRA data is permitted."
        )

    # --- Validation: source allow-list ---
    if source not in ALLOWED_SOURCES:
        log("warn", "REJECTED: source not in allow-list",
            run_id=run_id, isolate_id=isolate_id, source=source, allowed=list(ALLOWED_SOURCES))
        raise ValueError(
            f"Source '{source}' not in allowed sources: {sorted(ALLOWED_SOURCES)}. "
            "Only public NCBI SRA sources are permitted."
        )

    # --- Validation: accession format ---
    if not PUBLIC_ACCESSION_RE.match(accession):
        log("warn", "REJECTED: accession format invalid",
            run_id=run_id, isolate_id=isolate_id, accession=accession)
        raise ValueError(
            f"Accession '{accession}' does not match expected public SRA format. "
            "Expected SRR/ERR/DRR/SRX/SRS/SRP/ERP/DRP prefix."
        )

    # --- Resolve provenance ---
    provenance = resolve_source_and_license(accession, source)
    source_provenance = provenance["source_provenance"]
    license_str = provenance["license"]

    log("info", "Validation passed", run_id=run_id, isolate_id=isolate_id,
        source_provenance=source_provenance, license=license_str)

    # --- Record to DynamoDB ---
    record_to_dynamo(isolate_id, accession, source_provenance, license_str, run_id)

    # --- Return enriched event ---
    return {
        **event,
        "isolate_id": isolate_id,
        "accession": accession,
        "source": source,
        "source_provenance": source_provenance,
        "license": license_str,
        "organism": organism_label,
        "amrfinder_organism": amrfinder_organism,
        "validated": True,
    }
