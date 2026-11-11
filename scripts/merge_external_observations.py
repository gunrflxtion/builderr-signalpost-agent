#!/usr/bin/env python3
"""Merge external-connector observations into baseline profiles + envelopes.

Reads the baseline profiles/envelopes produced by run_competition_batch.py
plus the observation files produced by the free connector scripts, and
emits merged profiles + terminal envelopes with extra evidence modules:

  news, reviews, linkedin_profile, linkedin_jobs, youtube,
  brave_discovery (only when a Brave API key was configured), synthesis

Rules (precision is king):
- An observation is attached only when it is structurally valid AND its
  exact-entity proof is present. Anything else is rejected and counted,
  never published.
- A connector that produced nothing for a company is recorded as an
  explicit terminal state (not_found / not_applicable / source_error),
  never as a silent drop. Every input organisation still gets exactly
  one terminal envelope.
- Synthesis is deterministic templates over structured evidence only.
  No LLM, no third-party model, $0 third-party cost. Unknowns are stated
  as "not established by this run", never as proof of non-existence.
- Availability vocabulary: this file uses the kit's evidence statuses
  (available / not_found / not_applicable / source_error / blocked),
  which the envelope maps to terminal states (complete / not_found /
  not_applicable / source_error / blocked_policy). The competition
  contract's "not_available" is the same outcome as kit "not_found"
  (checked, absent) and "failed" is kit "source_error".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import evidence_terminal_state, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.evidence import evidence, utc_now  # noqa: E402

EXTERNAL_MODULES = [
    "news",
    "reviews",
    "linkedin_profile",
    "linkedin_jobs",
    "youtube",
    "brave_discovery",
    "synthesis",
]

NEWS_ENDPOINT = "https://news.google.com/rss/search"
LINKEDIN_TYPEAHEAD = "https://www.linkedin.com/jobs-guest/api/typeaheadHits"
LINKEDIN_JOBS_API = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
YOUTUBE_SEARCH = "https://www.youtube.com/results?search_query="


def read_jsonl(path: Path | None) -> list[dict]:
    if path is None or not Path(path).exists():
        return []
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def read_json(path: Path | None) -> dict:
    if path is None or not Path(path).exists():
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_hash(value: str) -> bool:
    return len(str(value or "")) == 64 and all(
        char in "0123456789abcdef" for char in str(value or "").casefold()
    )


def validate_observation(obs: dict) -> list[str]:
    """Minimal structural + exact-entity gate. Rights/mode are NOT judged
    here: the observation keeps its declared acquisition_mode and
    rights_status, and the evaluator decides what earns points."""
    reasons: list[str] = []
    if not str(obs.get("id") or "").strip():
        reasons.append("missing observation id")
    if not str(obs.get("organisation_number") or "").isdigit():
        reasons.append("missing or invalid organisation number")
    if not obs.get("source_url") or not (urlparse(str(obs.get("source_url"))).hostname or ""):
        reasons.append("missing or invalid source URL")
    if not obs.get("retrieved_at"):
        reasons.append("missing retrieval time")
    if not check_hash(str(obs.get("content_sha256") or "")):
        reasons.append("missing content hash")
    if not obs.get("exact_entity"):
        reasons.append("exact legal entity is not verified")
    if not obs.get("identity_proof"):
        reasons.append("missing exact-entity proof")
    return reasons


def fagfolk_slug(value: object) -> str:
    text = str(value or "").translate(
        str.maketrans({"ø": "o", "å": "a", "æ": "ae", "Ø": "O", "Å": "A", "Æ": "AE"})
    )
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return "-".join(re.findall(r"[a-z0-9]+", text))


def fmt_nok(value: object) -> str:
    try:
        return f"NOK {float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _ev_ref(module: str, record: dict) -> dict:
    return {
        "module": module,
        "source_url": record.get("source_url"),
        "retrieved_at": record.get("retrieved_at"),
        "content_sha256": record.get("content_sha256"),
    }


def build_synthesis(profile: dict, obs_by_platform: dict[str, list[dict]]) -> dict:
    """Deterministic summary from structured evidence only. Templates,
    no model. Every section cites the evidence it was built from."""
    org = str(profile.get("organisation_number") or "")
    name = str(profile.get("name") or "The company")
    ev = profile.get("evidence", {})
    sections: list[dict] = []

    identity = (
        f"{name} is a {profile.get('legal_form') or 'registered entity'} "
        f"registered in {profile.get('municipality') or 'Norway'} "
        f"(organisation number {org})."
    )
    if profile.get("employees") is not None:
        identity += f" The registry reports {profile['employees']} employees."
    if profile.get("industry_label"):
        industry = f"Its registered industry is {profile['industry_label']}"
        if profile.get("industry_code"):
            industry += f" ({profile['industry_code']})"
        identity += " " + industry + "."
    sections.append({
        "heading": "What the company does",
        "text": identity,
        "evidence": [_ev_ref("registry", ev.get("registry", {}))],
    })

    website = ev.get("website", {})
    web_value = website.get("value") or {}
    publishable = bool((web_value.get("identity_assessment") or {}).get("publishable", True))
    if website.get("status") == "available" and publishable and web_value.get("description"):
        desc = str(web_value["description"]).strip()
        if len(desc) > 400:
            desc = desc[:400].rstrip() + "…"
        activity_text = f"Its website describes the company as: “{desc}”."
        activity_refs = [_ev_ref("website", website)]
    elif website.get("status") == "available" and not publishable:
        activity_text = "A registry-linked website was fetched, but exact legal-entity identity was not established, so its description is not repeated here."
        activity_refs = [_ev_ref("website", website)]
    else:
        activity_text = "No publishable website description was available to this run."
        activity_refs = [_ev_ref("website", website)]
    sections.append({"heading": "Website description", "text": activity_text, "evidence": activity_refs})

    financials = ev.get("financials", {})
    records = (financials.get("value") or {}).get("records") or []
    if len(records) >= 2:
        latest, previous = records[0], records[1]
        bits = []
        for key, label in (("revenue", "Revenue"), ("annual_result", "Annual result")):
            new, old = latest.get(key), previous.get(key)
            if new is not None and old not in (None, 0):
                pct = (float(new) - float(old)) / abs(float(old)) * 100
                bits.append(
                    f"{label} changed from {fmt_nok(old)} ({previous.get('period')}) "
                    f"to {fmt_nok(new)} ({latest.get('period')}), {pct:+.1f}%"
                )
            elif new is not None:
                bits.append(f"{label} was {fmt_nok(new)} in {latest.get('period')}.")
        fin_text = " ".join(bits) if bits else f"Filed accounts cover {latest.get('period')}."
    elif len(records) == 1:
        latest = records[0]
        bits = []
        for key, label in (("revenue", "Revenue"), ("annual_result", "Annual result")):
            if latest.get(key) is not None:
                bits.append(f"{label} {fmt_nok(latest[key])}")
        fin_text = (
            f"The latest filed accounts ({latest.get('period')}) report "
            + ("; ".join(bits) if bits else "no revenue/result figures in the normalized record")
            + "."
        )
    else:
        fin_text = "No normalized annual-account record was returned; this does not mean the company has no accounts."
    sections.append({
        "heading": "What changed (annual accounts)",
        "text": fin_text,
        "evidence": [_ev_ref("financials", financials)],
    })

    ext_bits: list[str] = []
    ext_refs: list[dict] = []
    news = obs_by_platform.get("news", [])
    if news:
        publishers = sorted({str(item.get("publisher") or "") for item in news if item.get("publisher")})
        ext_bits.append(f"{len(news)} news mention(s) from {', '.join(publishers) or 'unknown publishers'} in the last 2 years")
        ext_refs.append(_ev_ref("news", {"source_url": news[0].get("source_url"), "retrieved_at": news[0].get("retrieved_at"), "content_sha256": news[0].get("content_sha256")}))
    reviews = [item for item in obs_by_platform.get("company_directory", []) if item.get("signal_type") == "review_summary"]
    if reviews:
        metrics = reviews[0].get("metrics") or {}
        ext_bits.append(f"Google aggregate rating {metrics.get('rating')}/5 from {metrics.get('review_count')} reviews")
        ext_refs.append(_ev_ref("reviews", {"source_url": reviews[0].get("source_url"), "retrieved_at": reviews[0].get("retrieved_at"), "content_sha256": reviews[0].get("content_sha256")}))
    jobs = obs_by_platform.get("linkedin", [])
    job_posts = [item for item in jobs if item.get("signal_type") == "job_posting"]
    if job_posts:
        ext_bits.append(f"{len(job_posts)} active LinkedIn job posting(s)")
        ext_refs.append(_ev_ref("linkedin_jobs", {"source_url": job_posts[0].get("source_url"), "retrieved_at": job_posts[0].get("retrieved_at"), "content_sha256": job_posts[0].get("content_sha256")}))
    handles = [item for item in jobs if item.get("signal_type") == "profile_handle"]
    if handles:
        ext_bits.append(f"verified LinkedIn company profile {handles[0].get('profile_url')}")
        ext_refs.append(_ev_ref("linkedin_profile", {"source_url": handles[0].get("source_url"), "retrieved_at": handles[0].get("retrieved_at"), "content_sha256": handles[0].get("content_sha256")}))
    yt = obs_by_platform.get("youtube", [])
    channels = [item for item in yt if item.get("signal_type") == "profile_handle"]
    if channels:
        ext_bits.append(f"verified YouTube channel {channels[0].get('profile_url')}")
        ext_refs.append(_ev_ref("youtube", {"source_url": channels[0].get("source_url"), "retrieved_at": channels[0].get("retrieved_at"), "content_sha256": channels[0].get("content_sha256")}))
    if ext_bits:
        ext_text = "External signals found: " + "; ".join(ext_bits) + "."
    else:
        ext_text = "No exact-entity external signals were found by this run's connectors."
    sections.append({"heading": "External footprint", "text": ext_text, "evidence": ext_refs})

    gaps: list[str] = []
    if website.get("status") != "available":
        gaps.append("no verified website")
    if not records:
        gaps.append("no annual accounts returned")
    if not job_posts and not handles:
        gaps.append("no LinkedIn presence established")
    elif handles and not job_posts:
        gaps.append("no active LinkedIn job postings found")
    if not reviews:
        gaps.append("no customer reviews found")
    if not news:
        gaps.append("no news mentions in the last 2 years")
    if not yt:
        gaps.append("no verified YouTube channel found")
    unknown_text = (
        "Not established by this run: " + "; ".join(gaps) + "."
        if gaps
        else "No major gaps: registry, accounts, website and external signals were all present."
    )
    sections.append({"heading": "What is not known", "text": unknown_text, "evidence": []})

    summary = " ".join(section["text"] for section in sections)
    return {
        "summary": summary,
        "sections": sections,
        "generated_by": "deterministic_synthesis_v1 (templates over structured evidence; no LLM, no third-party model)",
        "models": [],
    }


def module_record(field: str, status: str, source_url: str, value: dict, note: str | None = None) -> dict:
    content = canonical_json({"field": field, "status": status, "value": value})
    return evidence(
        field,
        status,  # type: ignore[arg-type]
        f"signalpost_agent_{field}",
        source_url,
        value=value,
        note=note,
        content_sha256=sha256_text(content),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge external observations into Signalpost profiles + envelopes.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--envelopes", required=True)
    parser.add_argument("--news")
    parser.add_argument("--reviews")
    parser.add_argument("--linkedin-handles")
    parser.add_argument("--linkedin-jobs")
    parser.add_argument("--youtube")
    parser.add_argument("--brave-profiles")
    parser.add_argument("--brave-report")
    parser.add_argument("--news-report")
    parser.add_argument("--reviews-report")
    parser.add_argument("--linkedin-discovery-report")
    parser.add_argument("--linkedin-jobs-report")
    parser.add_argument("--youtube-report")
    parser.add_argument("--output", required=True)
    parser.add_argument("--profiles-output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=100)
    parser.add_argument("--modules", default=",".join(EXTERNAL_MODULES))
    args = parser.parse_args()

    started = time.time()
    started_at = utc_now()
    profiles = read_jsonl(Path(args.profiles))
    envelopes = read_jsonl(Path(args.envelopes))
    if len(profiles) != args.expected_count or len(envelopes) != args.expected_count:
        raise SystemExit(
            f"Expected {args.expected_count} profiles/envelopes, got {len(profiles)}/{len(envelopes)}"
        )
    orgs = [str(row["organisation_number"]) for row in profiles]
    by_org = {org: row for org in orgs for row in [next(p for p in profiles if str(p["organisation_number"]) == org)]}

    # Brave output replaces profiles when it ran (it may promote a discovered site).
    brave_profiles = read_jsonl(Path(args.brave_profiles)) if args.brave_profiles else []
    brave_by_org = {str(row["organisation_number"]): row for row in brave_profiles}
    brave_report = read_json(Path(args.brave_report)) if args.brave_report else {}
    brave_ran = bool(brave_profiles) or bool(brave_report)

    def load_obs(path: str | None) -> list[dict]:
        return read_jsonl(Path(path)) if path else []

    raw_obs = {
        "news": load_obs(args.news),
        "reviews": load_obs(args.reviews),
        "linkedin_handles": load_obs(args.linkedin_handles),
        "linkedin_jobs": load_obs(args.linkedin_jobs),
        "youtube": load_obs(args.youtube),
    }
    reports = {
        "news": read_json(Path(args.news_report)) if args.news_report else {},
        "reviews": read_json(Path(args.reviews_report)) if args.reviews_report else {},
        "linkedin_discovery": read_json(Path(args.linkedin_discovery_report)) if args.linkedin_discovery_report else {},
        "linkedin_jobs": read_json(Path(args.linkedin_jobs_report)) if args.linkedin_jobs_report else {},
        "youtube": read_json(Path(args.youtube_report)) if args.youtube_report else {},
    }

    # Group + validate observations; rejected ones are counted, never attached.
    accepted: dict[str, list[dict]] = {org: [] for org in orgs}
    rejected: list[dict] = []
    seen_ids: set[str] = set()
    for stage, rows in raw_obs.items():
        for obs in rows:
            org = str(obs.get("organisation_number") or "")
            reasons = validate_observation(obs)
            if org not in accepted:
                reasons.append("organisation number not in this batch")
            if obs.get("id") in seen_ids:
                reasons.append("duplicate observation id")
            if reasons:
                rejected.append({"id": obs.get("id"), "organisation_number": org, "stage": stage, "reasons": reasons})
                continue
            seen_ids.add(str(obs.get("id")))
            accepted[org].append(obs)
    for org in orgs:
        accepted[org].sort(key=lambda item: (str(item.get("platform") or ""), str(item.get("signal_type") or ""), str(item.get("id") or "")))

    def obs_by_platform(org: str) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for item in accepted[org]:
            grouped.setdefault(str(item.get("platform") or ""), []).append(item)
        return grouped

    # Per-company connector outcomes, from reports where the connector publishes them.
    def report_rows(report: dict, key: str) -> dict[str, dict]:
        rows = report.get(key) or []
        return {str(row.get("organisation_number")): row for row in rows if isinstance(row, dict)}

    news_rows = report_rows(reports["news"], "company_results")
    reviews_rows = report_rows(reports["reviews"], "company_results")
    discovery_rows = report_rows(reports["linkedin_discovery"], "results")
    jobs_rows = report_rows(reports["linkedin_jobs"], "company_results")
    youtube_errors = {
        str(item.get("organisation_number"))
        for item in (reports["youtube"].get("errors") or [])
        if isinstance(item, dict)
    }

    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    module_counts: dict[str, dict[str, int]] = {m: {} for m in requested_modules}
    coverage = {
        "companies_with_external_observations": 0,
        "companies_with_two_or_more_platforms": 0,
        "companies_with_news": 0,
        "companies_with_reviews": 0,
        "companies_with_linkedin": 0,
        "companies_with_youtube": 0,
    }

    merged_profiles: list[dict] = []
    for org in orgs:
        profile = by_org[org]
        if brave_ran and org in brave_by_org:
            # Keep the Brave-promoted website evidence; merge everything else from baseline.
            brave_profile = brave_by_org[org]
            profile.setdefault("evidence", {}).update(brave_profile.get("evidence", {}))
        ev = profile.setdefault("evidence", {})
        platforms = obs_by_platform(org)
        news_obs = platforms.get("news", [])
        review_obs = [i for i in platforms.get("company_directory", []) if i.get("signal_type") == "review_summary"]
        handle_obs = [i for i in platforms.get("linkedin", []) if i.get("signal_type") == "profile_handle"]
        job_obs = [i for i in platforms.get("linkedin", []) if i.get("signal_type") == "job_posting"]
        yt_obs = platforms.get("youtube", [])

        if accepted[org]:
            coverage["companies_with_external_observations"] += 1
            if len(platforms) >= 2:
                coverage["companies_with_two_or_more_platforms"] += 1
        if news_obs:
            coverage["companies_with_news"] += 1
        if review_obs:
            coverage["companies_with_reviews"] += 1
        if handle_obs or job_obs:
            coverage["companies_with_linkedin"] += 1
        if yt_obs:
            coverage["companies_with_youtube"] += 1

        # --- news ---
        if "news" in requested_modules:
            row = news_rows.get(org, {})
            if row.get("error"):
                status, note = "source_error", f"Google News RSS failed: {row['error']}"
                url = NEWS_ENDPOINT
            elif news_obs:
                status, note, url = "available", None, str(news_obs[0].get("source_url"))
            elif reports["news"]:
                status, note, url = "not_found", "RSS search returned no exact-title match in the last 2 years.", NEWS_ENDPOINT
            else:
                status, note, url = "source_error", "News connector stage did not produce a report.", NEWS_ENDPOINT
            value = {"mention_count": len(news_obs), "publishers": sorted({str(i.get("publisher") or "") for i in news_obs if i.get("publisher")})}
            ev["news"] = module_record("news", status, url, value, note)
        # --- reviews ---
        if "reviews" in requested_modules:
            row = reviews_rows.get(org, {})
            if row.get("error"):
                status, note = "source_error", f"Fagfolkguiden fetch failed: {row['error']}"
            elif review_obs:
                status, note = "available", None
            elif row.get("accepted") is True:
                status, note = "not_found", "Exact directory page found, but no aggregate rating was embedded."
            elif row:
                status, note = "not_found", "No exact Fagfolkguiden directory page matched this company."
            elif reports["reviews"]:
                status, note = "not_found", "No exact Fagfolkguiden directory page matched this company."
            else:
                status, note = "source_error", "Reviews connector stage did not produce a report."
            metrics = (review_obs[0].get("metrics") or {}) if review_obs else {}
            url = str(review_obs[0].get("source_url")) if review_obs else f"https://www.fagfolkguiden.no/bedrift/{fagfolk_slug(profile.get('name'))}-{org}"
            value = {"rating": metrics.get("rating"), "review_count": metrics.get("review_count"), "rated": bool(review_obs)}
            ev["reviews"] = module_record("reviews", status, url, value, note)
        # --- linkedin profile ---
        if "linkedin_profile" in requested_modules:
            row = discovery_rows.get(org, {})
            if row.get("errors"):
                status, note = "source_error", f"LinkedIn discovery errors: {'; '.join(str(e) for e in row['errors'])[:240]}"
            elif handle_obs:
                status, note = "available", None
            elif row:
                status, note = "not_found", "No exact LinkedIn company profile matched (typeahead + domain/location gates)."
            elif reports["linkedin_discovery"]:
                status, note = "not_found", "No exact LinkedIn company profile matched (typeahead + domain/location gates)."
            else:
                status, note = "source_error", "LinkedIn discovery stage did not produce a report."
            url = str(handle_obs[0].get("profile_url")) if handle_obs else LINKEDIN_TYPEAHEAD
            value = {"profile_url": str(handle_obs[0].get("profile_url")) if handle_obs else None,
                     "linkedin_company_ids": sorted({str(i) for h in handle_obs for i in (h.get("linkedin_company_ids") or [])})}
            ev["linkedin_profile"] = module_record("linkedin_profile", status, url, value, note)
        # --- linkedin jobs ---
        if "linkedin_jobs" in requested_modules:
            row = jobs_rows.get(org, {})
            if row.get("errors"):
                status, note = "source_error", f"LinkedIn jobs errors: {'; '.join(str(e) for e in row['errors'])[:240]}"
            elif job_obs:
                status, note = "available", None
            elif row:
                status, note = "not_found", "Verified LinkedIn handle exists, but no exact job postings were found."
            elif handle_obs:
                status, note = "source_error", "LinkedIn jobs stage did not produce a report for a company with a verified handle."
            else:
                status, note = "not_applicable", "No verified LinkedIn handle, so jobs were not queried."
            url = str(job_obs[0].get("source_url")) if job_obs else LINKEDIN_JOBS_API
            value = {"job_count": len(job_obs), "job_titles": sorted({str((i.get("metrics") or {}).get("title") or "") for i in job_obs if (i.get("metrics") or {}).get("title")})[:10]}
            ev["linkedin_jobs"] = module_record("linkedin_jobs", status, url, value, note)
        # --- youtube ---
        if "youtube" in requested_modules:
            website = ev.get("website", {})
            web_value = website.get("value") or {}
            eligible = website.get("status") == "available" and bool((web_value.get("identity_assessment") or {}).get("publishable", True))
            channels = [i for i in yt_obs if i.get("signal_type") == "profile_handle"]
            posts = [i for i in yt_obs if i.get("signal_type") == "public_post"]
            if channels:
                status, note = "available", None
            elif org in youtube_errors:
                status, note = "source_error", "YouTube search failed for this company."
            elif not eligible:
                status, note = "not_applicable", "No publishable website, so no YouTube channel search was attempted."
            elif reports["youtube"]:
                status, note = "not_found", "YouTube search returned no exact-name channel with repeated results."
            else:
                status, note = "source_error", "YouTube connector stage did not produce a report."
            url = str(channels[0].get("profile_url")) if channels else YOUTUBE_SEARCH
            metrics = {}
            for item in yt_obs:
                if item.get("signal_type") == "profile_metrics":
                    metrics = item.get("metrics") or {}
            value = {"channel_url": str(channels[0].get("profile_url")) if channels else None,
                     "matched_posts": len(posts), "recent_views_total": metrics.get("recent_views_total")}
            ev["youtube"] = module_record("youtube", status, url, value, note)
        # --- brave discovery ---
        if "brave_discovery" in requested_modules:
            brave_ev = (brave_by_org.get(org, {}).get("evidence", {}) or {}).get("website_discovery", {})
            if not brave_ran:
                ev["brave_discovery"] = module_record(
                    "brave_discovery", "not_applicable", BRAVE_ENDPOINT, {"queried": False},
                    "Brave Search API key not configured; discovery was not attempted (free tier needs a key).",
                )
            elif brave_ev:
                bstatus = brave_ev.get("status") or "not_found"
                value = brave_ev.get("value") or {}
                ev["brave_discovery"] = module_record(
                    "brave_discovery",
                    "available" if bstatus == "available" else "not_found" if bstatus == "not_found" else "source_error",
                    str(value.get("independent_page_url") or brave_ev.get("source_url") or BRAVE_ENDPOINT),
                    {"discovered_url": value.get("independent_page_url"), "candidate_count": value.get("candidate_count")},
                    brave_ev.get("note"),
                )
            else:
                ev["brave_discovery"] = module_record(
                    "brave_discovery", "source_error", BRAVE_ENDPOINT, {"queried": True},
                    "Brave discovery stage did not return a website_discovery record for this company.",
                )
        # --- synthesis (always) ---
        if "synthesis" in requested_modules:
            synthesis = build_synthesis(profile, platforms)
            ev["synthesis"] = evidence(
                "synthesis",
                "available",
                "deterministic_synthesis",
                f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}",
                value=synthesis,
                note="Template synthesis over structured evidence only; no LLM, no third-party model.",
                content_sha256=sha256_text(canonical_json(synthesis)),
            )

        for module in requested_modules:
            record = ev.get(module)
            if record is not None:
                module_counts[module][record.get("status")] = module_counts[module].get(record.get("status"), 0) + 1
        profile["external_observations"] = accepted[org]
        merged_profiles.append(profile)

    completed_at = utc_now()
    baseline_modules = [m for m in envelopes[0].get("modules", {}).keys()] if envelopes else []
    merged_envelopes: list[dict] = []
    for envelope in envelopes:
        org = str(envelope.get("organisation_number"))
        profile = next(p for p in merged_profiles if str(p["organisation_number"]) == org)
        merged = terminal_envelope(
            profile, run_id=args.run_id,
            modules=list(dict.fromkeys(baseline_modules + requested_modules)),
            started_at=envelope.get("started_at") or started_at,
            completed_at=completed_at,
        )
        merged_envelopes.append(merged)

    validation = validate_envelopes(merged_envelopes, args.expected_count)

    def write_jsonl(path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        temporary.replace(path)

    write_jsonl(Path(args.profiles_output), merged_profiles)
    write_jsonl(Path(args.output), merged_envelopes)

    batch_requests = 0
    batch_report = read_json(Path(args.envelopes).parent / "batch-report.json")
    if batch_report:
        batch_requests = int((batch_report.get("operations") or {}).get("requests") or 0)
    connector_attempts = (
        len(news_rows) + len(reviews_rows) + len(discovery_rows) + len(jobs_rows)
        + int(reports["youtube"].get("searched") or 0)
    )
    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "runtime_ms": int((time.time() - started) * 1000),
        "expected_count": args.expected_count,
        "emitted_envelopes": len(merged_envelopes),
        "modules": list(dict.fromkeys(baseline_modules + requested_modules)),
        "module_status_counts": module_counts,
        "observations": {
            "accepted": sum(len(v) for v in accepted.values()),
            "rejected": len(rejected),
            "rejections": rejected[:50],
            "by_platform": {
                platform: sum(1 for org in orgs for item in accepted[org] if item.get("platform") == platform)
                for platform in sorted({str(i.get("platform")) for org in orgs for i in accepted[org]})
            },
        },
        "coverage": coverage,
        "operations": {
            "requests": batch_requests + connector_attempts,
            "request_accounting": "baseline batch requests + one counted attempt per company per connector stage (news, reviews, LinkedIn discovery, LinkedIn jobs company rows, YouTube searches); LinkedIn jobs detail verifications are additional requests inside the jobs stage",
            "runtime_ms": int((time.time() - started) * 1000),
            "third_party_cost_usd": 0,
        },
        "validation": validation,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
