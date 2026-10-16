#!/usr/bin/env python3
"""Версионированное происхождение фактов без сети и повторного скоринга.

``raw_refs`` — явный адаптер записанных ответов, см. references/evidence-contract.md.
Даты публикации/состояния принимаются только от проверенной карты raw; паспорта
и время скачивания сами по себе не доказывают дату факта. Legacy JSON неизменен.
"""
from __future__ import annotations

import contextvars
import copy
import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any, TypedDict

# Preserve flat installed imports and legacy absolute-file entry points.
try:
    from sibling_imports import load_sibling as _load_sibling
except ModuleNotFoundError as _error:
    if _error.name != "sibling_imports":
        raise
    import importlib.util as _import_util
    import sys as _import_sys
    from pathlib import Path as _ImportPath
    _spec = _import_util.spec_from_file_location(
        "sibling_imports", _ImportPath(__file__).resolve().with_name("sibling_imports.py"))
    _helper = _import_util.module_from_spec(_spec)
    _spec.loader.exec_module(_helper)
    # Publish only the complete, stateless bootstrap. Concurrent bootstrap calls
    # all select the same helper; dependency loads use Python's module locks.
    _load_sibling = _import_sys.modules.setdefault("sibling_imports", _helper).load_sibling


class RawRef(TypedDict):
    response_id: str | None
    sha256: str | None
    locator: str | None
    availability: str


class Claim(TypedDict):
    claim_id: str
    subject_inn: str
    field: str
    value: Any
    unit: str | None
    knowledge: str
    reason_code: str | None
    source_id: str
    origin_family: str | None
    upstream_dataset: str | None
    relation: str
    raw_ref: RawRef
    retrieved_at: str
    published_at: str | None
    first_observed_at: str | None
    effective_at: str | None
    period: dict | None
    valid_until: str | None
    freshness_policy: str
    transformation: str
    depends_on: list[str]
    search_scope: dict | None
    coverage_complete: bool | None


class Evidence(TypedDict):
    schema_version: int
    claims: dict[str, Claim]
    conflicts: list[dict]
    applicability: dict
    rules_version: str


_RESOURCE_CACHE: dict[str, tuple[object, dict]] = {}


def _resource(filename: str) -> dict:
    local = Path(__file__).resolve().parents[1] / "data" / filename
    if local.is_file():
        stat = local.stat()
        stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        previous = _RESOURCE_CACHE.get(filename)
        if previous and previous[0] == stamp:
            return previous[1]
        value = json.loads(local.read_text(encoding="utf-8"))
        _RESOURCE_CACHE[filename] = (stamp, value)
        return value
    return json.loads(resources.files("inn_check_ru_data").joinpath(filename).read_text(encoding="utf-8"))


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _same_value(left: Any, right: Any) -> bool:
    """JSON semantic equality, with numeric equivalence but distinct booleans."""
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_value(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    return left == right


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="auto").replace("+00:00", "Z") if value else None


_PUBLICATION_SEMANTICS = contextvars.ContextVar("inn_check_publication_semantics", default=None)


def _publication_available(claim: dict, instant: datetime, semantics: dict | None) -> bool:
    """Trusted adapter assertion for this exact raw-backed claim; never infer it.

    This only establishes availability at the cutoff. It does not override
    missing raw, conflicts, expiry, source coverage or other knowledge checks.
    """
    assertion = semantics.get(claim["claim_id"]) if isinstance(semantics, dict) else None
    published = claim.get("published_at")
    if not (isinstance(assertion, dict) and assertion.get("confirmed") is True
            and assertion.get("meaning") == "public_availability"
            and isinstance(assertion.get("reference"), str) and assertion["reference"].strip()
            and claim["raw_ref"]["availability"] == "available"
            and isinstance(published, str)
            and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?Z", published)):
        return False
    available = _instant(published)
    return available is not None and available <= instant


def _year(value: Any) -> int | None:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{4}", str(value)):
        return None
    year = int(value)
    return year if 1900 <= year <= 9998 else None


def _period(year: int | None) -> dict | None:
    return {"start": "%04d-01-01" % year, "end": "%04d-12-31" % year} if year else None


def _valid_period(value: Any) -> bool:
    try:
        return (isinstance(value, dict) and set(value) == {"start", "end"}
                and date.fromisoformat(value["start"]) <= date.fromisoformat(value["end"]))
    except (ValueError, TypeError, KeyError):
        return False


def _get(root: Any, path: str) -> Any:
    cur = root
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _pointer(root: Any, pointer: str) -> Any:
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("unmapped_locator")
    cur = root
    if not pointer:
        return cur
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise ValueError("unmapped_locator")
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", key):
                raise ValueError("unmapped_locator")
            try:
                cur = cur[int(key)]
            except IndexError:
                raise ValueError("unmapped_locator") from None
        elif isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            raise ValueError("unmapped_locator")
    return cur


def _transform(value: Any, name: str) -> Any:
    if name == "identity_v1":
        return value
    if name in ("integer_v1", "number_v1"):
        if isinstance(value, bool) or value is None:
            raise ValueError("transformation_invalid")
        try:
            number = float(str(value).replace("\u00a0", "").replace(" ", "").replace(",", ".").replace("руб.", "").replace("₽", ""))
        except (ValueError, TypeError):
            raise ValueError("transformation_invalid") from None
        if not math.isfinite(number) or (name == "integer_v1" and not number.is_integer()):
            raise ValueError("transformation_invalid")
        return int(number) if name == "integer_v1" else number
    raise ValueError("transformation_unsupported")


def _available(fetch: dict, source: str) -> bool:
    av = fetch.get("_доступность", {})
    state = av.get(source) if isinstance(av, dict) else None
    holder = fetch.get(source)
    if isinstance(holder, dict) and str(holder.get("статус") or "").lower().startswith(("не проверено", "проверено частично")):
        return False
    if state is not None and source != "fin":
        _состояние_блока = _load_sibling("profiles")._состояние_блока
        state = _состояние_блока(fetch, source)[0]
        if state not in ("ok", "пусто"):
            return False
    if state is None:
        # Legacy data remains observable, but is never evidence of complete search.
        return True
    if isinstance(state, dict):
        state = state.get("состояние")
    return isinstance(state, str) and state.lower() in ("ok", "ок", "проверено", "пусто")


def _negative(value: Any) -> bool:
    return (value is False or isinstance(value, (list, dict)) and not value
            or isinstance(value, str) and value.strip().lower() in
            {"нет", "false", "0", "не выявлено", "отсутствует", "не найдено", "не числится", "none", "null", "no", "-", "—"})


def _claim_id(c: dict) -> str:
    # Value disambiguates legacy/untraced candidates and corrected same-locator inputs.
    return "sha256:" + _digest({k: c[k] for k in (
        "subject_inn", "field", "source_id", "raw_ref", "period", "transformation", "value")})


def _scoped_pointer(data: Any, locator: str, subject_locator: str, subject: str) -> Any:
    """Resolve only within the validated subject record, including nested IDs."""
    result = _pointer(data, locator)
    subject_parent = subject_locator.rsplit("/", 1)[0]
    if subject_parent and locator != subject_parent and not locator.startswith(subject_parent + "/"):
        raise ValueError("foreign_subject")
    tokens = locator[1:].split("/") if locator else []
    for depth in range(len(tokens)):
        ancestor = _pointer(data, "/" + "/".join(tokens[:depth])) if depth else data
        if isinstance(ancestor, dict):
            for identity in ("inn", "инн", "ИНН"):
                if ancestor.get(identity) is not None and str(ancestor[identity]) != subject:
                    raise ValueError("foreign_subject")
    return result


_RAW_JSON_CACHE = contextvars.ContextVar("inn_check_raw_json_cache", default=None)


def _decoded_response(body: bytes):
    cache = _RAW_JSON_CACHE.get()
    if cache is None:
        return json.loads(body)
    if body not in cache:
        cache[body] = json.loads(body)
    return cache[body]


def _raw_mapping(refs: dict, field: str, source: str, subject: str, value: Any,
                 mapping: dict, now: datetime) -> tuple[RawRef, dict, Any]:
    response_id = mapping.get("response_id")
    response = refs.get("responses", {}).get(response_id)
    if not isinstance(response, dict):
        raise ValueError("missing_response")  # noqa: TRY004 -- one public ValueError(code) boundary
    if response.get("source_id") != source:
        raise ValueError("source_mismatch")
    request = response.get("request")
    if (response.get("subject_inn") != subject or not isinstance(request, dict)
            or request.get("subject_inn") != subject):
        raise ValueError("foreign_subject")
    body = response.get("body")
    if not isinstance(body, bytes):
        raise ValueError("raw_bytes_required")  # noqa: TRY004 -- adapter schema error
    if hashlib.sha256(body).hexdigest() != response.get("sha256"):
        raise ValueError("raw_hash_mismatch")
    try:
        data = _decoded_response(body)
    except (ValueError, UnicodeError):
        raise ValueError("raw_json_invalid") from None
    try:
        raw_subject = _pointer(data, mapping.get("subject_locator"))
    except ValueError:
        raise ValueError("foreign_subject") from None
    if str(raw_subject) != subject:
        raise ValueError("foreign_subject")
    value_locator = mapping.get("locator")
    raw_value = _scoped_pointer(data, value_locator, mapping["subject_locator"], subject)
    transformed = _transform(raw_value, mapping.get("transformation", "identity_v1"))
    # Replay the declared conversion on a legacy typed input as well; never add
    # an unrecorded conversion after validating the response candidate.
    if not mapping.get("candidate") and (field.endswith("численность_сотрудников")
                                        or field.startswith("финансы.отчётность_по_годам.")):
        value = _transform(value, mapping.get("transformation", "identity_v1"))
    if not mapping.get("candidate") and not _same_value(transformed, value):
        raise ValueError("transformation_mismatch: " + field)
    return {"response_id": response_id, "sha256": response["sha256"],
            "locator": mapping["locator"], "availability": "available"}, response, transformed


def _manual_observation(fetch: dict, source: str, meta: dict, now: datetime) -> dict | None:
    """Bind a human declaration to its stored block, never to registry raw bytes.

    A calendar date has day precision and is not an exact observation timestamp.
    Import time and the declared observation date therefore remain separate.
    """
    if meta.get("relation") != "manual_observation" or "observation_ref" not in meta:
        return None
    from urllib.parse import urlsplit

    sources = _load_sibling("sources")

    block = fetch.get(source)
    ref = meta["observation_ref"]
    if (source not in {"суды", "фссп", "залоги"} or not isinstance(block, dict)
            or block.get("инн") != fetch.get("инн") or block.get("ввод") != "браузер"
            or not isinstance(ref, dict) or ref.get("source_id") != source
            or ref.get("locator") != "/fetch/" + source
            or not isinstance(ref.get("run_id"), str)
            or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", ref["run_id"])
            or type(ref.get("revision")) is not int or ref["revision"] < 1
            or ref.get("block_sha256") != _digest(block)):
        raise ValueError("invalid_manual_observation")
    parsed = urlsplit(block.get("url", ""))
    hosts = {urlsplit(sources.SOURCES[source].get("документация", "")).hostname,
             urlsplit(sources.SOURCES[source].get("probe", {}).get("url", "")).hostname}
    hosts = {host.removeprefix("www.") for host in hosts if host}
    if (parsed.scheme != "https" or (parsed.hostname or "").removeprefix("www.") not in hosts
            or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError("invalid_manual_observation")
    observed_on = date.fromisoformat(block.get("дата_проверки", ""))
    imported_at = _instant(meta.get("imported_at"))
    expiry = _instant(block.get("expires_at"))
    if (observed_on > now.date() or imported_at is None or imported_at > now
            or block.get("expires_at") is not None and expiry is None):
        raise ValueError("invalid_manual_observation")
    for flag in ("pagination_complete", "полно"):
        if flag in block and type(block[flag]) is not bool:
            raise ValueError("invalid_manual_observation")
    complete = (block.get("pagination_complete", block.get("полно"))
                if "pagination_complete" in block or "полно" in block
                else True if block.get("полнота") in {"полно", "полная", "complete"} else None)
    if (block.get("outcome") != "completed" or block.get("статус") != "проверено"
            or block.get("pagination_complete") is False or block.get("полно") is False
            or block.get("полнота") in {"частично", "incomplete"}):
        complete = False
    query = block.get("параметры_поиска") if block.get("scope_declared") is True else None
    scope = meta.get("search_scope")
    if (meta.get("coverage_complete") is not complete or
            query and (not isinstance(scope, dict) or scope.get("subject_inn") != fetch["инн"]
                       or scope.get("query") != query) or not query and scope is not None):
        raise ValueError("invalid_manual_observation")
    return {"subject_inn": fetch["инн"], "query": query,
            "manual_observation": {"block_ref": copy.deepcopy(ref), "source_url": block["url"],
                "observed_on": observed_on.isoformat(), "date_precision": "day",
                "imported_at": _iso(imported_at), "expires_at": _iso(expiry),
                "freshness": "explicit_expiry" if expiry else "unknown"}}


def _make_claim(fetch: dict, field: str, value: Any, spec: dict, refs: dict,
                mapping: dict | None, now: datetime, period: dict | None = None) -> Claim:
    subject, source = str(fetch.get("инн") or ""), spec["source_id"]
    if mapping is not None:
        source = mapping.get("source_id", source)
    raw: RawRef = {"response_id": None, "sha256": None, "locator": None, "availability": "raw_unavailable"}
    response, meta = {}, mapping or copy.deepcopy((fetch.get("_evidence_scope") or {}).get(field, {}))
    manual = _manual_observation(fetch, source, meta, now) if mapping is None else None
    transformation = meta.get("transformation", "identity_v1")
    if mapping is not None:
        raw, response, value = _raw_mapping(refs, field, source, subject, value, mapping, now)
    elif refs.get("responses") and manual is None:
        raw["availability"] = "unmapped_locator"
    dates = {}
    for name in ("published_at", "effective_at", "first_observed_at"):
        parsed = _instant(meta.get(name))
        locator = meta.get("date_locators", {}).get(name)
        # A date label without a checked source locator is not publication evidence.
        if parsed and locator and response:
            observed = _instant(_scoped_pointer(_decoded_response(response["body"]), locator,
                                               meta["subject_locator"], subject))
            if observed != parsed:
                raise ValueError("date_mapping_mismatch")
        elif name == "first_observed_at":
            parsed = _instant(response.get(name))
        else:
            parsed = None
        dates[name] = _iso(parsed) if parsed and parsed <= now else None
    retrieved = _instant(response.get("retrieved_at"))
    if retrieved and retrieved > now:
        raise ValueError("future_retrieval")
    if mapping is None:
        # An observation made during this adaptation is not proof of historical publication.
        dates["first_observed_at"] = _iso(now) if value is not None else None
    if manual is not None:
        dates["first_observed_at"] = None  # declared day is retained in search_scope
        retrieved = _instant(manual["manual_observation"]["imported_at"])
    period = meta.get("period", period)
    if period is not None and not _valid_period(period):
        raise ValueError("invalid_period")
    policy_id = spec.get("freshness_policy", "unknown_v1")
    policy = _resource("evidence_policy.json")["freshness"][policy_id]
    valid_until = None
    if policy["ttl_seconds"] and dates.get(policy["basis"]):
        valid_until = _instant(dates[policy["basis"]]) + timedelta(seconds=policy["ttl_seconds"])
    if manual is not None:
        policy_id = "unknown_v1"
        policy = _resource("evidence_policy.json")["freshness"][policy_id]
        valid_until = _instant(manual["manual_observation"]["expires_at"])
    knowledge, reason = "known", None
    is_staff = field.endswith("численность_сотрудников")
    is_money = field.startswith("финансы.отчётность_по_годам.")
    if (is_staff or is_money) and value is not None:
        try:
            if mapping is None:
                transformation = "integer_v1" if is_staff else "number_v1"
                value = _transform(value, transformation)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or is_staff and (value < 0 or not float(value).is_integer())):
                raise ValueError("invalid_field_type")
        except ValueError:
            knowledge, reason = "unknown", "schema_invalid"
    if value is None or value == "" or spec.get("derived"):
        knowledge, reason = "unknown", "missing_required_field"
    elif not _available(fetch, source):
        historical_positive = raw["availability"] == "available" and dates["effective_at"] and not _negative(value)
        if not historical_positive:
            knowledge, reason = "unknown", "source_unavailable"
    if knowledge == "known" and (_negative(value) or spec.get("requires_scope_for_zero") and value == 0):
        scope = meta.get("search_scope")
        if (meta.get("coverage_complete") is not True or not isinstance(scope, dict)
                or scope.get("subject_inn") != subject or not scope.get("query")):
            knowledge, reason = "unknown", "coverage_incomplete"
    if knowledge == "known" and policy["ttl_seconds"]:
        if valid_until is None:
            knowledge, reason = "unknown", "revision_unknown"
        elif now >= valid_until:
            knowledge, reason = "unknown", "stale"
    if knowledge == "known" and raw["availability"] != "available":
        reason = "raw_unavailable"
    if knowledge == "known" and manual is not None and valid_until and now >= valid_until:
        knowledge, reason = "unknown", "stale"
    c: Claim = {
        "claim_id": "", "subject_inn": subject, "field": field, "value": copy.deepcopy(value),
        "unit": spec.get("unit"), "knowledge": knowledge, "reason_code": reason,
        "source_id": source, "origin_family": None if meta.get("relation") == "manual_observation" else spec.get("origin_family"),
        "upstream_dataset": meta.get("upstream_dataset"),
        "relation": "derived" if spec.get("derived") else meta.get("relation", "legacy_untraced" if mapping is None else "primary"),
        "raw_ref": raw, "retrieved_at": _iso(retrieved or now), **dates,
        "period": copy.deepcopy(period), "valid_until": _iso(valid_until),
        "freshness_policy": policy_id, "transformation": transformation,
        "depends_on": [], "search_scope": manual if manual is not None else copy.deepcopy(meta.get("search_scope")),
        "coverage_complete": meta.get("coverage_complete")}
    c["claim_id"] = _claim_id(c)
    return c


def _staff_period(fetch: dict, source: str) -> dict | None:
    holder = fetch.get(source, {})
    if not isinstance(holder, dict):
        return None
    years = [holder[k] for k in ("среднесписочная_за_год", "численность_год") if holder.get(k) is not None]
    if not years and source == "дампы_фнс" and holder.get("год") is not None:
        years = [holder["год"]]
    valid = [_year(y) for y in years]
    return _period(valid[0]) if valid and all(valid) and len(set(valid)) == 1 else None


def _conflicts(claims: dict[str, Claim]) -> list[dict]:
    groups: dict[bytes, list[Claim]] = {}
    for c in claims.values():
        if c["value"] is not None and c["knowledge"] in ("known", "conflict"):
            key = _canonical([c["subject_inn"], c["field"], c["period"]])
            groups.setdefault(key, []).append(c)
    conflicts = []
    for group in groups.values():
        if any(not _same_value(c["value"], group[0]["value"]) for c in group[1:]):
            ids = sorted(c["claim_id"] for c in group)
            conflicts.append({"field": group[0]["field"], "period": group[0]["period"],
                              "candidate_claim_ids": ids, "selected_claim_id": None,
                              "reason_code": "conflicting_values"})
            for c in group:
                c["knowledge"], c["reason_code"] = "conflict", None
    return sorted(conflicts, key=_canonical)


def _financial_claims(fetch: dict, policy: dict, refs: dict, now: datetime) -> list[Claim]:
    records = _get(fetch, "финансы.отчётность_по_годам")
    if not isinstance(records, list):
        records = []
    years = [_year(rec.get("год")) for rec in records if isinstance(rec, dict)]
    current = max((y for y in years if y), default=None)
    # Missing previous year remains explicit; no year is inferred from the clock.
    if current and current - 1 not in years:
        records = records + [{"год": current - 1}]
    if not records:
        records = [{}]
    out = []
    for record in records:
        if not isinstance(record, dict):
            record = {}
        year = _year(record.get("год"))
        for code, names in policy["financial_rows"].items():
            value = _get(record, "строки." + code)
            if value is None:
                value = next((record[n] for n in names if record.get(n) is not None), None)
            field = "финансы.отчётность_по_годам.%s.строки.%s" % (year or "unknown", code)
            spec = {"source_id": "финансы", "origin_family": "fns_girbo", "unit": "thousand_RUB",
                    "freshness_policy": "annual_financial_v1"}
            mappings = refs.get("fields", {}).get(field) or [None]
            out.extend(_make_claim(fetch, field, value, spec, refs, m, now, _period(year)) for m in mappings)
    return out


def _bind_dependencies(claims: dict[str, Claim], policy: dict) -> None:
    by_field: dict[str, list[Claim]] = {}
    for c in claims.values():
        by_field.setdefault(c["field"], []).append(c)
    annual = [c for c in claims.values() if c["field"].startswith("финансы.отчётность_по_годам.") and c["period"]]
    current = max((int(c["period"]["start"][:4]) for c in annual), default=None)
    for rule, inputs in policy["financial_rule_inputs"].items():
        for c in by_field.get("fin." + rule, []):
            deps = []
            for item in inputs:
                if ":" in item:
                    code, rel = item.split(":")
                    year = current if rel == "current" else current - 1 if current else None
                    deps.extend(by_field.get("финансы.отчётность_по_годам.%s.строки.%s" % (year or "unknown", code), []))
                elif item == "численность_сотрудников":
                    # Match the scorer's precedence, but retain unavailable alternatives as dependencies.
                    deps.extend(by_field.get("риски.численность_сотрудников", []))
                    deps.extend(by_field.get("дампы_фнс.численность_сотрудников", []))
                    deps.extend(by_field.get("риски.среднесписочная_за_год", []))
                    deps.extend(by_field.get("риски.численность_год", []))
                    deps.extend(by_field.get("дампы_фнс.численность_год", []))
                    deps.extend(by_field.get("дампы_фнс.год", []))
                else:
                    deps.extend(by_field.get(item, []))
            c["depends_on"] = sorted({d["claim_id"] for d in deps})
            staff = next((d for source in ("риски", "дампы_фнс") for d in deps
                          if d["field"] == source + ".численность_сотрудников" and d["value"] is not None), None)
            if (rule == "транзитный_профиль" and current and staff
                    and (staff["period"] is None or int(staff["period"]["start"][:4]) != current)):
                c["reason_code"] = "period_mismatch"


def _insert_claim(claims: dict[str, Claim], claim: Claim) -> None:
    previous = claims.get(claim["claim_id"])
    if previous is not None and _canonical(previous) != _canonical(claim):
        raise ValueError("inconsistent_duplicate_claim")
    claims[claim["claim_id"]] = claim


def _applicability(fetch: dict, claims: dict[str, Claim], policy: dict) -> dict:
    supplied = fetch.get("_reporting")
    supplied = supplied if isinstance(supplied, dict) else {}
    # Reuse the collector's public INN validator; arbitrary supplied metadata
    # cannot turn a legal entity into an IP and erase financial requirements.
    validate_inn = _load_sibling("fetch_counterparty").validate_inn
    valid_inn = validate_inn(fetch.get("инн", ""))
    subject = "ip" if valid_inn and len(valid_inn) == 12 else "legal_entity" if valid_inn else "unknown"
    form = supplied.get("reporting_form") or _get(fetch, "финансы.форма_отчётности")
    if form is None and subject == "ip":
        form = "ip"
    evidence = sorted(c["claim_id"] for c in claims.values()
                      if c["field"] == "финансы.форма_отчётности" and c["value"] == form
                      and c["knowledge"] == "known" and c["raw_ref"]["availability"] == "available")
    rules = {}
    for field in policy["fields"]:
        if field.startswith("fin."):
            support = policy["forms"].get(form, {"rule_status": "unknown", "reason_code": "unsupported_form"})
            if form == "restricted":
                support = {"rule_status": "unknown", "reason_code": "not_disclosed"}
            elif form != "ip" and not evidence or form == "ip" and subject != "ip":
                support = {"rule_status": "unknown", "reason_code": "unsupported_form"}
            elif evidence and form == "simplified":
                # A simplified label does not prove any section is unavailable.
                # Disclosed compatible inputs remain eligible, including red CHA.
                rule_claim = next(c for c in claims.values() if c["field"] == field)
                financial_deps = [claims[d] for d in rule_claim["depends_on"]
                                  if claims[d]["source_id"] == "финансы"]
                disclosed = financial_deps and all(d["knowledge"] == "known" for d in financial_deps)
                support = {"rule_status": "applicable" if disclosed else "unknown",
                           "reason_code": None if disclosed else "missing_required_field"}
            rules[field] = {"status": support["rule_status"], "reason_code": support["reason_code"]}
        else:
            rules[field] = {"status": "applicable", "reason_code": None}
    sources = sorted({c["source_id"] for c in claims.values() if c["source_id"] != "fin"})
    coverage = {}
    for source in sources:
        observed = [c for c in claims.values() if c["source_id"] == source]
        state = "known" if _available(fetch, source) and all(c["knowledge"] == "known" for c in observed) else "unknown"
        coverage[source] = {"state": state, "reason_code": None if state == "known" else "coverage_incomplete"}
    return {"subject_type": subject, "reporting_form": form,
            "form_version": supplied.get("form_version") or _get(fetch, "финансы.версия_формы"),
            "form_evidence_ids": evidence, "source_coverage": coverage, "rules": rules}


def enrich(fetch: dict, *, now: str, raw_refs: dict | None = None) -> dict:
    """Enrich using a per-call decoded-byte cache, released before returning."""
    token = _RAW_JSON_CACHE.set({})
    try:
        return _enrich(fetch, now=now, raw_refs=raw_refs)
    finally:
        _RAW_JSON_CACHE.reset(token)


def _enrich(fetch: dict, *, now: str, raw_refs: dict | None = None) -> dict:
    """Глубокая копия fetch + evidence v1; без сети, файловых записей и score().

    Некорректный предоставленный raw отвергается ValueError(code), а legacy без
    raw сохраняет наблюдаемое значение с raw_unavailable. См. публичный адаптер.
    """
    instant = _instant(now)
    if instant is None:
        raise ValueError("invalid_now: UTC ISO 8601 with timezone required")
    if not isinstance(fetch, dict):
        raise ValueError("schema_invalid: fetch must be dict")  # noqa: TRY004 -- public contract
    previous = fetch.get("_evidence")
    if previous is not None:
        if not isinstance(previous, dict) or previous.get("schema_version") != 1:
            raise ValueError("unsupported_schema")
        if validate_evidence(fetch):
            raise ValueError("schema_invalid: previous evidence")
    policy = _resource("evidence_policy.json")
    profiles = _resource("profiles_ru.json")
    expected = {p for s in profiles["сигналы"].values() for p in s["откуда"]}
    if not expected <= policy["fields"].keys():
        raise ValueError("unsupported_profile_path")
    refs = raw_refs or {}
    if not isinstance(refs, dict) or not isinstance(refs.get("fields", {}), dict) or not isinstance(refs.get("responses", {}), dict):
        raise ValueError("schema_invalid: raw_refs")  # noqa: TRY004 -- public contract
    unknown_mappings = set(refs.get("fields", {})) - set(policy["fields"])
    if any(not field.startswith("финансы.отчётность_по_годам.") for field in unknown_mappings):
        raise ValueError("unsupported_field_mapping")
    out = copy.deepcopy(fetch)
    claims: dict[str, Claim] = {}
    for field, spec in policy["fields"].items():
        value = _get(fetch, field)
        period = _staff_period(fetch, spec["source_id"]) if field.endswith("численность_сотрудников") else None
        mappings = refs.get("fields", {}).get(field) or [None]
        if not isinstance(mappings, list) or any(m is not None and not isinstance(m, dict) for m in mappings):
            raise ValueError("schema_invalid: field mappings")
        for mapping in mappings:
            c = _make_claim(fetch, field, value, spec, refs, mapping, instant, period)
            _insert_claim(claims, c)
    for c in _financial_claims(fetch, policy, refs, instant):
        _insert_claim(claims, c)
    used_fields = {c["field"] for c in claims.values()}
    if not refs.get("fields", {}).keys() <= used_fields:
        raise ValueError("unsupported_field_mapping")
    if previous and raw_refs is None:
        # Re-evaluation has no new response bytes. Retain previously verified
        # candidates only for unchanged field values/periods; never fabricate refs.
        fields = {c["field"] for c in claims.values() if not c["field"].startswith("fin.")}
        for field in fields:
            fresh = [c for c in claims.values() if c["field"] == field]
            prior = [c for c in previous["claims"].values() if c["field"] == field]
            if prior and any(c["raw_ref"]["availability"] == "available" for c in prior) and any(_same_value(a["value"], b["value"]) and a["period"] == b["period"]
                             for a in fresh for b in prior):
                for c in fresh:
                    del claims[c["claim_id"]]
                for original in prior:
                    c = copy.deepcopy(original)
                    if ((_instant(c["retrieved_at"]) and _instant(c["retrieved_at"]) > instant
                            or _instant(c["first_observed_at"]) and _instant(c["first_observed_at"]) > instant)
                            and not _publication_available(c, instant, _PUBLICATION_SEMANTICS.get())):
                        c["knowledge"], c["reason_code"] = "unknown", "revision_unknown"
                    expiry = _instant(c["valid_until"])
                    if expiry and instant >= expiry and c["knowledge"] == "known":
                        c["knowledge"], c["reason_code"] = "unknown", "stale"
                    if not _available(fetch, c["source_id"]) and not (
                            c["raw_ref"]["availability"] == "available" and c["effective_at"]
                            and not _negative(c["value"])):
                        c["knowledge"], c["reason_code"] = "unknown", "source_unavailable"
                    _insert_claim(claims, c)
    if previous:
        for cid, claim in claims.items():
            prior = previous["claims"].get(cid)
            if prior and _instant(prior["first_observed_at"]):
                earliest = _instant(prior["first_observed_at"])
                current_observed = _instant(claim["first_observed_at"])
                # Re-enrichment is not a new historical observation. Preserve the
                # actual timestamp even when it lies after the requested cutoff.
                claim["first_observed_at"] = _iso(min(earliest, current_observed)
                    if raw_refs is not None and current_observed else earliest)
                if (_instant(claim["first_observed_at"]) > instant
                        and not _publication_available(claim, instant, _PUBLICATION_SEMANTICS.get())):
                    claim["knowledge"], claim["reason_code"] = "unknown", "revision_unknown"
    conflicts = _conflicts(claims)
    _bind_dependencies(claims, policy)
    app = _applicability(fetch, claims, policy)
    for c in claims.values():
        rule = app["rules"].get(c["field"])
        if c["field"].startswith("fin.") and rule and rule["status"] == "not_applicable":
            c["knowledge"], c["reason_code"] = "not_applicable", rule["reason_code"]
    out["_evidence"] = {"schema_version": 1, "claims": dict(sorted(claims.items())),
        "conflicts": conflicts, "applicability": app,
        "rules_version": _digest({"profiles": profiles, "policy": policy, "canon": _resource("canon_ru.json")})}
    issues = validate_evidence(out)
    if issues:
        raise ValueError("schema_invalid: " + issues[0]["code"])
    return out



_EVALUATION_NOW = contextvars.ContextVar("inn_check_evaluated_at", default=None)


def prepare(fetch: dict, *, now: str | None = None) -> dict:
    """Shared strict adapter for legacy API callers, with explicit provenance gaps."""
    if now is None:
        now = _EVALUATION_NOW.get() or _iso(datetime.now(timezone.utc))
    return enrich(fetch, now=now)


def normalized_financial_input(fetch: dict) -> dict:
    """Copy authoritative known leaves into scorer input; conflicting leaves stay null."""
    out = copy.deepcopy(fetch)
    evidence = out.get("_evidence", {})
    by_field = {}
    for c in evidence.get("claims", {}).values():
        by_field.setdefault(c["field"], []).append(c)

    def value(field):
        candidates = by_field.get(field, [])
        known = [c for c in candidates if c["knowledge"] == "known"]
        return known[0]["value"] if known and not any(c["knowledge"] == "conflict" for c in candidates) else None

    for source in ("риски", "дампы_фнс"):
        if isinstance(out.get(source), dict):
            out[source]["численность_сотрудников"] = value(source + ".численность_сотрудников")
    if isinstance(out.get("егрюл"), dict):
        out["егрюл"]["уставный_капитал"] = value("егрюл.уставный_капитал")
    records = _get(out, "финансы.отчётность_по_годам")
    if isinstance(records, list):
        policy = _resource("evidence_policy.json")
        for row in records:
            if not isinstance(row, dict):
                continue
            year = _year(row.get("год"))
            row.setdefault("строки", {})
            for code, aliases in policy["financial_rows"].items():
                row["строки"][code] = value("финансы.отчётность_по_годам.%s.строки.%s" % (year or "unknown", code))
                for alias in aliases:
                    row.pop(alias, None)
    return out


def bind_financial(fetch: dict, fin: dict) -> dict:
    """Bind Phase 0's evaluated statuses, without calculating another risk flag."""
    input_identity = _load_sibling("fin_scoring").input_identity
    bound = fin.get("_input_identity") == input_identity(normalized_financial_input(fetch))
    out = copy.deepcopy(fetch)
    claims = out["_evidence"]["claims"]
    evaluations = (fin.get("оценки_сигналов") or {}) if bound else {}
    profile = (fin.get("финансовый_профиль") or {}) if bound else {}
    staff_source = profile.get("численность_источник")
    current = profile.get("года") or []
    current_year = _year(current[0]) if current else None
    for c in claims.values():
        if not c["field"].startswith("fin."):
            continue
        rule = out["_evidence"]["applicability"]["rules"][c["field"]]
        ev = evaluations.get(c["field"].split(".", 1)[1], {})
        period_mismatch = c["reason_code"] == "period_mismatch"
        support_complete = bool(c["depends_on"])
        if c["field"] == "fin.транзитный_профиль" and bound:
            # Consume proof support from the actual scorer branch. Do not
            # duplicate its growth calculation or infer absence from missing staff.
            revenue_support = (["2110:%s" % current_year, "2110:%s" % (current_year - 1)]
                               if current_year else None)
            if revenue_support and ev.get("достаточные_зависимости") == revenue_support:
                c["depends_on"] = [cid for cid in c["depends_on"]
                                   if claims[cid]["source_id"] == "финансы"]
                period_mismatch = False  # staff period is not part of this proof
            elif staff_source:
                # Every supplied year alias participates in period validation;
                # absent aliases are alternate field names, not additional facts.
                years = [staff_source + "." + name for name in
                         ("среднесписочная_за_год", "численность_год")
                         if _get(fetch, staff_source + "." + name) is not None]
                if not years:
                    # Keep a missing year explicit. Undated staff cannot support
                    # a cross-period assertion even if legacy arithmetic accepts it.
                    years = [staff_source + ".численность_год"]
                staff_field = staff_source + ".численность_сотрудников"
                c["depends_on"] = [cid for cid in c["depends_on"] if
                    claims[cid]["source_id"] == "финансы" or
                    claims[cid]["field"] in [staff_field, *years]]
                selected_staff = [claims[cid] for cid in c["depends_on"]
                                  if claims[cid]["field"] == staff_field]
                support_complete = {staff_field, *years} <= {
                    claims[cid]["field"] for cid in c["depends_on"]}
                period_mismatch = bool(current_year and any(
                    d["period"] != _period(current_year) for d in selected_staff))
        deps = [claims[cid] for cid in c["depends_on"]]
        if rule["status"] == "not_applicable":
            c["knowledge"], c["reason_code"] = "not_applicable", rule["reason_code"]
        elif rule["status"] == "unknown":
            c["knowledge"], c["reason_code"] = "unknown", rule["reason_code"]
        elif any(d["knowledge"] == "conflict" for d in deps):
            c["knowledge"], c["reason_code"] = "conflict", "conflicting_values"
        elif period_mismatch:
            c["knowledge"], c["reason_code"] = "unknown", "period_mismatch"
        elif not support_complete or any(d["knowledge"] != "known" for d in deps):
            c["knowledge"], c["reason_code"] = "unknown", "missing_required_field"
        elif not bound:
            c["knowledge"], c["reason_code"] = "unknown", "financial_input_mismatch"
        elif ev.get("статус") in ("найден", "отсутствует"):
            c["knowledge"], c["reason_code"] = "known", None
            c["value"] = ev["статус"] == "найден"
        else:
            c["knowledge"], c["reason_code"] = "unknown", "missing_required_field"
        c["period"] = _period(current_year)
    # IDs include the actual derived value and period. No downstream input
    # refers to financial output claims, so rematerializing cannot dangle refs.
    derived = [cid for cid, c in claims.items() if c["field"].startswith("fin.")]
    for cid in derived:
        claim = claims.pop(cid)
        claim["claim_id"] = _claim_id(claim)
        claims[claim["claim_id"]] = claim
    out["_evidence"]["claims"] = dict(sorted(claims.items()))
    return out


def decision_metadata(fetch: dict, assessment: dict, profile_id: str, catalog: dict) -> dict:
    """Describe the existing resolver's required-rule coverage, not a second resolver."""
    prof = catalog["профили"].get(profile_id, {})
    required_sources = set(prof.get("источники_обязательные") or [])
    overrides = prof.get("severity_override") or {}
    killers = set(prof.get("deal_killers") or [])
    signals = assessment.get("_signals") or []
    required, supported, missing, ids = [], [], [], set()
    claims = fetch["_evidence"]["claims"]
    for signal in signals:
        sid = signal["id"]
        desc = catalog["сигналы"][sid]
        sources = {p.split(".", 1)[0].replace("fin", "финансы") for p in desc["откуда"]}
        severity = "🔴" if sid in killers else overrides.get(sid, desc.get("severity_по_умолчанию"))
        needed = sid in killers or overrides.get(sid) == "🔴" or bool(sources & required_sources) and severity != "⚪"
        if signal["статус"] == "не применимо":
            continue
        used = signal.get("claim_ids", [])
        if signal["статус"] in ("найден", "отсутствует"):
            ids.update(used)
        if not needed:
            continue
        required.append(sid)
        if signal["статус"] in ("найден", "отсутствует"):
            supported.append(sid)
        else:
            related = [claims[cid] for cid in used if cid in claims]
            missing.append({"rule_id": sid, "claim_ids": used,
                            "reason_codes": sorted({c["reason_code"] or c["knowledge"] for c in related}) or ["missing_required_field"]})
    facts_only = prof.get("формулировка") == "факты"
    can = not facts_only and not missing and bool(required) and assessment.get("светофор") is not None
    return {"schema_version": 1, "rules_version": fetch["_evidence"]["rules_version"],
            "decision_status": "facts_only" if facts_only else "supported" if can else "insufficient_evidence",
            "can_recommend": can, "missing_evidence": missing,
            "next_checks": [{"rule_id": m["rule_id"], "reason_codes": m["reason_codes"]} for m in missing],
            "claim_ids": sorted(ids), "coverage": {"numerator": len(supported), "denominator": len(required),
                "supported_rules": sorted(supported), "required_rules": sorted(required)}}


def evaluate(fetch: dict, *, profile: str, now: str, publication_semantics: dict | None = None) -> dict:
    """One authoritative pass; publication_semantics is a trusted adapter input.

    Historical replay supplies its saved per-claim publication assertions here.
    Ordinary calls always use an empty context, including calls nested in replay.
    """
    _версия = _load_sibling("evidence_pack")._версия
    score = _load_sibling("fin_scoring").score
    profiles = _load_sibling("profiles")
    resolve, source_states = profiles.resolve, profiles.source_states
    if profile not in _resource("profiles_ru.json")["профили"]:
        raise ValueError("unknown_profile")
    publication_context = copy.deepcopy(publication_semantics)
    token = _EVALUATION_NOW.set(now)
    publication_token = _PUBLICATION_SEMANTICS.set(publication_context)
    try:
        enriched = enrich(fetch, now=now)
        fin = score(enriched)
        enriched = bind_financial(enriched, fin)
        assessment = resolve(enriched, fin, profile)
        if "_decision" not in assessment:
            raise ValueError("assessment_unavailable")
        decision = assessment["_decision"]
        enriched["_decision"] = copy.deepcopy(decision)
        enriched["_evidence"]["source_states"] = source_states(enriched)
        return {"fetch": enriched, "fin": fin, "assessment": assessment,
                "metadata": {"schema_version": 1, "engine_version": _версия(),
                             "rules_version": enriched["_evidence"]["rules_version"],
                             "evaluated_at": _iso(_instant(now)), "decision": copy.deepcopy(decision)}}
    finally:
        _PUBLICATION_SEMANTICS.reset(publication_token)
        _EVALUATION_NOW.reset(token)


def validate_evidence(fetch: dict) -> list[dict]:
    """Структура/subject/ссылки/циклы; байты повторно проверяет enrich(raw_refs=...)."""
    issues = []
    def issue(code: str, claim_id: str | None = None) -> None:
        issues.append({"code": code, "claim_id": claim_id})
    evidence = fetch.get("_evidence") if isinstance(fetch, dict) else None
    if not isinstance(evidence, dict):
        return [{"code": "missing_evidence", "claim_id": None}]
    if evidence.get("schema_version") != 1:
        return [{"code": "unsupported_schema", "claim_id": None}]
    claims = evidence.get("claims")
    if not isinstance(claims, dict):
        return [{"code": "schema_invalid", "claim_id": None}]
    # Structural schema-1 limit is immutable; business policy cannot invalidate
    # an already materialized revision. Changing it requires a new schema.
    max_deps = 32
    required = set(Claim.__annotations__)
    for cid, c in claims.items():
        if not isinstance(c, dict) or set(c) != required:
            issue("schema_invalid", cid)
            continue
        try:
            valid_id = cid == c["claim_id"] and cid == _claim_id(c)
        except (TypeError, ValueError):
            valid_id = False
        if not valid_id:
            issue("claim_id_mismatch", cid)
        if c["subject_inn"] != str(fetch.get("инн") or ""):
            issue("foreign_subject", cid)
        if c["knowledge"] not in ("known", "unknown", "not_applicable", "conflict"):
            issue("schema_invalid", cid)
        raw = c["raw_ref"]
        if not isinstance(raw, dict) or set(raw) != set(RawRef.__annotations__):
            issue("invalid_raw_ref", cid)
        elif raw["availability"] == "available":
            if (not isinstance(raw["response_id"], str) or not raw["response_id"]
                    or not isinstance(raw["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", raw["sha256"])
                    or not isinstance(raw["locator"], str) or raw["locator"] and not raw["locator"].startswith("/")):
                issue("invalid_raw_ref", cid)
        elif raw["availability"] not in ("raw_unavailable", "unmapped_locator") or any(raw[k] is not None for k in ("response_id", "sha256", "locator")):
            issue("invalid_raw_ref", cid)
        if c["period"] is not None and not _valid_period(c["period"]):
            issue("invalid_period", cid)
        for name in ("retrieved_at", "published_at", "first_observed_at", "effective_at", "valid_until"):
            if c[name] is not None and (_instant(c[name]) is None or not c[name].endswith("Z")):
                issue("invalid_timestamp", cid)
        deps = c["depends_on"]
        if not isinstance(deps, list) or len(deps) > max_deps or any(not isinstance(d, str) for d in deps):
            issue("dependency_limit", cid)
        else:
            for dep in deps:
                if dep not in claims:
                    issue("dangling_reference", cid)
    visiting, visited = set(), set()
    def visit(cid: str) -> None:
        if cid in visiting:
            issue("dependency_cycle", cid)
            return
        if cid in visited:
            return
        visiting.add(cid)
        c = claims.get(cid)
        deps = c.get("depends_on", []) if isinstance(c, dict) else []
        if isinstance(deps, list):
            for dep in deps:
                if isinstance(dep, str) and dep in claims:
                    visit(dep)
        visiting.remove(cid)
        visited.add(cid)
    for cid in claims:
        visit(cid)
    return sorted(issues, key=lambda e: (e["code"], e["claim_id"] or ""))
