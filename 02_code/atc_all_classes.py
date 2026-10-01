"""Purpose:
    Add ATC3, ATC4, ATC count, and lookup status to the formulary panel.

Process:
    1. Read the panel NDC column and normalize each value to NDC11.
    2. Use the cached RxNav results and query missing or retryable NDCs.
    3. Add ATC3, ATC4, n_atc, and ATC_status in chunks.
    4. Replace the original panel only after the complete temporary output
       has been written successfully.

Input:
    data/formulary/formulary_panel_with_company_id.csv
    D:/pharma/WHO ATC-DDD 2024-07-31.csv
    D:/pharma/ndc_atc_api_lookup.csv
    D:/pharma/ndc_atc_all_cache.json

Output:
    data/formulary/formulary_panel_with_company_id.csv
"""

import json
import os
import stat
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PHARMA_DIR = Path(r"D:\pharma")

PANEL = PROJECT_ROOT / "data" / "formulary" / "formulary_panel_with_company_id.csv"
WHO_FILE = PHARMA_DIR / "WHO ATC-DDD 2024-07-31.csv"
LEGACY_CACHE = PHARMA_DIR / "ndc_atc_api_lookup.csv"
ATC_CACHE = PHARMA_DIR / "ndc_atc_all_cache.json"
OUTPUT = PANEL
OUTPUT_DIR = OUTPUT.parent
TEMP_OUTPUT = OUTPUT.with_name(OUTPUT.name + ".building")

CHUNK_SIZE = 250_000
MAX_WORKERS = 8
REQUEST_TIMEOUT = 25
REQUEST_ATTEMPTS = 2
NEGATIVE_CACHE_TTL_DAYS = 90
ATC_COLUMNS = [
    # "ATC1", "ATC1_name", "ATC2", "ATC2_name",
    "ATC3",
    # "ATC3_name",
    "ATC4",
    # "ATC4_name",
    "n_atc", "ATC_status",
]


def normalize_ndc11(value):
    """Normalize CMS 11-digit NDCs and segment-aware 10-digit NDCs."""
    if value is None or pd.isna(value):
        return ""

    raw = str(value).strip()
    if not raw:
        return ""

    if "-" in raw:
        parts = raw.split("-")
        if not all(part.isdigit() for part in parts):
            return ""
        lengths = tuple(len(part) for part in parts)
        digits = "".join(parts)
        if lengths == (5, 4, 1):
            return digits[:9] + "0" + digits[9:]
        if lengths == (5, 3, 2):
            return digits[:5] + "0" + digits[5:]
        if lengths == (4, 4, 2):
            return "0" + digits
        if lengths == (5, 4, 2):
            return digits
        return ""

    if not raw.isdigit():
        return ""
    if len(raw) == 11:
        return raw
    # An unsegmented 10-digit NDC is ambiguous; do not guess which segment
    # needs a leading zero.
    return ""


def utc_now_iso():
    """Return a timezone-aware UTC timestamp for cache entries."""
    return datetime.now(timezone.utc).isoformat()


def cache_entry_needs_retry(info):
    """Retry failed or expired negative results instead of freezing misses."""
    status = str(info.get("status", ""))
    if status in {"OK"}:
        return False
    if status in {"NO_ATC", "NDC_NOT_FOUND"}:
        cached_at = info.get("cached_at")
        if not cached_at:
            return True
        try:
            cached_time = datetime.fromisoformat(cached_at)
            age_days = (datetime.now(timezone.utc) - cached_time).days
            return age_days >= NEGATIVE_CACHE_TTL_DAYS
        except (TypeError, ValueError):
            return True
    # HTTP errors, timeouts, partial responses, and legacy fallback entries
    # are retried on every run.
    return True


def load_all_cache():
    """Load valid cache entries and normalize their NDC keys."""
    if not ATC_CACHE.exists():
        return {}
    with ATC_CACHE.open("r", encoding="utf-8") as source:
        raw_cache = json.load(source)

    cache = {}
    for key, value in raw_cache.items():
        normalized_key = normalize_ndc11(key)
        if normalized_key and isinstance(value, dict):
            value.setdefault("atc_list", [])
            value.setdefault("status", "UNKNOWN")
            cache[normalized_key] = value
    return cache


def save_all_cache(cache):
    """Atomically save the all-ATC cache so an interrupted write cannot corrupt it."""
    ATC_CACHE.parent.mkdir(parents=True, exist_ok=True)
    temp_cache = ATC_CACHE.with_name(ATC_CACHE.name + ".tmp")
    with temp_cache.open("w", encoding="utf-8") as target:
        json.dump(cache, target, ensure_ascii=False)
    if ATC_CACHE.exists():
        ATC_CACHE.chmod(ATC_CACHE.stat().st_mode | stat.S_IWRITE)
    os.replace(temp_cache, ATC_CACHE)


def load_legacy_cache():
    """Load known single-ATC results for use as explicitly marked fallbacks."""
    if not LEGACY_CACHE.exists():
        return {}
    legacy = {}
    for chunk in pd.read_csv(
        LEGACY_CACHE,
        dtype=str,
        usecols=lambda name: name in {"NDC_panel", "status", "ATC4"},
        chunksize=100_000,
        on_bad_lines="error",
    ):
        for row in chunk.itertuples(index=False, name=None):
            record = dict(zip(chunk.columns, row))
            key = normalize_ndc11(record.get("NDC_panel"))
            atc_value = record.get("ATC4")
            if key and record.get("status") == "OK" and pd.notna(atc_value):
                codes = [code.strip() for code in str(atc_value).split(";") if code.strip()]
                if codes:
                    legacy[key] = codes
    return legacy


def normalize_api_list(value):
    """Normalize API fields that may be represented as one object or a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def request_json(url):
    """Request JSON with bounded retries and return the response or an error."""
    last_error = "REQUEST_ERROR"
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                return response.json(), ""
            last_error = f"HTTP{response.status_code}"
        except (requests.RequestException, ValueError) as error:
            last_error = f"REQUEST_ERROR:{type(error).__name__}"
        if attempt + 1 < REQUEST_ATTEMPTS:
            time.sleep(1 + attempt)
    return None, last_error


def make_atc_record(code, name_map):
    """Create one ATC hierarchy record from an ATC4 code."""
    return {
        "atc4": code,
        "atc3": code[:4],
        "atc2": code[:3],
        "atc1": code[:1],
        "atc4_name": name_map.get(code, ""),
        "atc3_name": name_map.get(code[:4], ""),
        "atc2_name": name_map.get(code[:3], ""),
        "atc1_name": name_map.get(code[:1], ""),
    }


def query_all(ndc11, name_map, legacy_cache):
    """Query every RxCUI found for an NDC and retain partial results on errors."""
    dash = f"{ndc11[:5]}-{ndc11[5:9]}-{ndc11[9:11]}"
    result = {
        "ndc": ndc11,
        "atc_list": [],
        "drug": "",
        "status": "UNKNOWN",
        "cached_at": utc_now_iso(),
    }

    ndc_url = f"https://rxnav.nlm.nih.gov/REST/ndcproperties.json?id={quote(dash)}"
    ndc_payload, error = request_json(ndc_url)
    if error:
        result["status"] = error
        result["error"] = error
        return apply_legacy_fallback(result, legacy_cache, name_map)

    property_list = ndc_payload.get("ndcPropertyList", {}).get("ndcProperty", [])
    properties = normalize_api_list(property_list)
    if not properties:
        result["status"] = "NDC_NOT_FOUND"
        return result

    rxcuis = sorted({str(item.get("rxcui")) for item in properties if item.get("rxcui")})
    if not rxcuis:
        result["status"] = "NO_RXCUI"
        return result

    atc_by_code = {}
    errors = []
    successful_rxcuis = 0
    for rxcui in rxcuis:
        class_url = (
            "https://rxnav.nlm.nih.gov/REST/rxclass/class/byRxcui.json"
            f"?rxcui={quote(rxcui)}&relaSource=ATC"
        )
        class_payload, error = request_json(class_url)
        if error:
            errors.append(f"{rxcui}:{error}")
            continue

        successful_rxcuis += 1
        class_list = class_payload.get("rxclassDrugInfoList", {}).get("rxclassDrugInfo", [])
        for item in normalize_api_list(class_list):
            min_concept = item.get("minConcept") or {}
            if not result["drug"]:
                result["drug"] = min_concept.get("name", "")
            class_concept = item.get("rxclassMinConceptItem") or {}
            code = str(class_concept.get("classId", "")).strip()
            class_type = str(class_concept.get("classType", ""))
            if "ATC1-4" in class_type and len(code) == 5 and code not in atc_by_code:
                atc_by_code[code] = make_atc_record(code, name_map)

    result["atc_list"] = list(atc_by_code.values())
    if errors:
        result["status"] = "PARTIAL_ERROR" if result["atc_list"] else "ERROR"
        result["error"] = ";".join(errors)
        return apply_legacy_fallback(result, legacy_cache, name_map)
    if successful_rxcuis == 0:
        result["status"] = "ERROR"
        return apply_legacy_fallback(result, legacy_cache, name_map)
    result["status"] = "OK" if result["atc_list"] else "NO_ATC"
    return result


def apply_legacy_fallback(result, legacy_cache, name_map):
    """Use a known single ATC only when the all-class lookup did not complete."""
    if not result.get("atc_list"):
        codes = legacy_cache.get(result["ndc"], [])
        if codes:
            result["atc_list"] = [make_atc_record(code, name_map) for code in codes]
            result["status"] = "FALLBACK_SINGLE_ATC"
    return result


def build_mapping(cache):
    """Build the per-NDC output columns from cache records."""
    mapping = {}
    for ndc_key, info in cache.items():
        atcs = info.get("atc_list", []) or []
        if atcs:
            mapping[ndc_key] = {
                # "ATC1": ";".join(item.get("atc1", "") for item in atcs),
                # "ATC1_name": ";".join(item.get("atc1_name", "") for item in atcs),
                # "ATC2": ";".join(item.get("atc2", "") for item in atcs),
                # "ATC2_name": ";".join(item.get("atc2_name", "") for item in atcs),
                "ATC3": ";".join(item.get("atc3", "") for item in atcs),
                # "ATC3_name": ";".join(item.get("atc3_name", "") for item in atcs),
                "ATC4": ";".join(item.get("atc4", "") for item in atcs),
                # "ATC4_name": ";".join(item.get("atc4_name", "") for item in atcs),
                "n_atc": len(atcs),
                "ATC_status": info.get("status", "UNKNOWN"),
            }
        else:
            mapping[ndc_key] = {
                **{column: "" for column in ATC_COLUMNS if column not in {"n_atc", "ATC_status"}},
                "n_atc": 0,
                "ATC_status": info.get("status", "UNKNOWN"),
            }
    return mapping


def collect_panel_ndcs():
    """Scan the panel strictly and return raw-to-normalized NDC keys."""
    raw_ndcs = set()
    for chunk in pd.read_csv(
        PANEL,
        chunksize=CHUNK_SIZE,
        dtype={"NDC": str},
        usecols=["NDC"],
        low_memory=False,
        on_bad_lines="error",
    ):
        raw_ndcs.update(chunk["NDC"].dropna().unique())

    raw_to_key = {raw: normalize_ndc11(raw) for raw in raw_ndcs}
    invalid_count = sum(not key for key in raw_to_key.values())
    valid_keys = sorted({key for key in raw_to_key.values() if key})
    return raw_to_key, valid_keys, invalid_count


def main():
    """Run the cached lookup and stream the enriched panel to disk."""
    start_time = time.time()
    for required in (PANEL, WHO_FILE):
        if not required.exists():
            raise FileNotFoundError(f"Required input file not found: {required}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PHARMA_DIR.mkdir(parents=True, exist_ok=True)

    who = pd.read_csv(WHO_FILE, dtype=str, usecols=["atc_code", "atc_name"])
    who["atc_code"] = who["atc_code"].str.strip()
    who["atc_name"] = who["atc_name"].fillna("").str.strip()
    name_map = dict(zip(who["atc_code"], who["atc_name"]))

    print("[1] Scanning NDCs from the base panel...")
    raw_to_key, all_keys, invalid_count = collect_panel_ndcs()
    print(f"    Unique raw NDCs: {len(raw_to_key):,}")
    print(f"    Unique valid NDC11 keys: {len(all_keys):,}")
    print(f"    Invalid or ambiguous NDC values: {invalid_count:,}")

    cache = load_all_cache()
    legacy_cache = load_legacy_cache()
    to_query = [
        key for key in all_keys
        if key not in cache or cache_entry_needs_retry(cache[key])
    ]
    print(f"[2] All-ATC cache entries: {len(cache):,}")
    print(f"    Legacy single-ATC fallbacks: {len(legacy_cache):,}")
    print(f"    NDCs to query or retry: {len(to_query):,}")

    if to_query:
        completed = 0
        last_save = time.time()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(query_all, key, name_map, legacy_cache): key
                for key in to_query
            }
            for future in as_completed(futures):
                result = future.result()
                key = normalize_ndc11(result.get("ndc"))
                result["cached_at"] = utc_now_iso()
                cache[key] = result
                completed += 1
                if completed <= 5:
                    print(
                        f"    {key}: {result['status']}, "
                        f"{len(result.get('atc_list', []))} ATC class(es)"
                    )
                if completed % 100 == 0:
                    print(f"    Queried/retried {completed:,}/{len(to_query):,}")
                if time.time() - last_save >= 300:
                    save_all_cache(cache)
                    last_save = time.time()
        save_all_cache(cache)

    mapping = build_mapping(cache)
    mapped_columns = {
        column: {key: value[column] for key, value in mapping.items()}
        for column in ATC_COLUMNS
    }

    # A previous output remains intact unless the new output completes fully.
    if TEMP_OUTPUT.exists():
        TEMP_OUTPUT.unlink()
    row_count = 0
    n_atc_dist = Counter()
    atc_status_counts = Counter()
    first_chunk = True

    print("[3] Streaming the enriched panel to a temporary output...")
    for chunk in pd.read_csv(
        PANEL,
        chunksize=CHUNK_SIZE,
        dtype={"NDC": str},
        low_memory=False,
        on_bad_lines="error",
    ):
        prior_atc_columns = [
            column for column in chunk.columns
            if column.startswith("ATC") or column in {"n_atc", "NDC11"}
        ]
        if prior_atc_columns:
            chunk = chunk.drop(columns=prior_atc_columns)

        ndc_keys = chunk["NDC"].map(raw_to_key).fillna("")
        for column in ATC_COLUMNS:
            chunk[column] = ndc_keys.map(mapped_columns[column])
        missing_ndc = ndc_keys.eq("")
        chunk.loc[missing_ndc, "ATC_status"] = "INVALID_OR_MISSING_NDC"
        chunk["n_atc"] = pd.to_numeric(chunk["n_atc"], errors="coerce").fillna(0).astype("int16")
        chunk["ATC_status"] = chunk["ATC_status"].fillna("NOT_IN_LOOKUP_CACHE")

        row_count += len(chunk)
        n_atc_dist.update(chunk["n_atc"].value_counts().to_dict())
        atc_status_counts.update(chunk["ATC_status"].value_counts().to_dict())
        chunk.to_csv(TEMP_OUTPUT, mode="a", index=False, header=first_chunk)
        first_chunk = False
        if row_count % 5_000_000 < CHUNK_SIZE:
            print(f"    Written {row_count:,} rows")

    if first_chunk:
        raise ValueError("The base panel contains no data rows; output was not replaced.")

    os.replace(TEMP_OUTPUT, OUTPUT)
    print(f"[4] Completed: {row_count:,} rows")
    print(f"    ATC count distribution: {dict(sorted(n_atc_dist.items()))}")
    print(f"    ATC lookup statuses: {dict(sorted(atc_status_counts.items()))}")
    print(f"    Output: {OUTPUT} ({OUTPUT.stat().st_size / 1e9:.2f} GB)")
    print(f"    Elapsed time: {(time.time() - start_time) / 60:.1f} minutes")


if __name__ == "__main__":
    main()
