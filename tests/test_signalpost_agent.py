"""Tests for the Signalpost agent additions: merge + deterministic synthesis.

These cover only the new code (scripts/merge_external_observations.py).
The kit's own suite (tests/test_poc.py) is untouched and must still pass.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MERGE = ROOT / "scripts" / "merge_external_observations.py"


def load_merge_module():
    spec = importlib.util.spec_from_file_location("merge_external_observations", MERGE)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "src"))
    spec.loader.exec_module(module)
    return module


MERGE_MOD = load_merge_module()


def good_obs(org: str, platform: str, signal: str, url: str) -> dict:
    return {
        "id": f"{platform}-{signal}-{org}",
        "organisation_number": org,
        "platform": platform,
        "signal_type": signal,
        "source_url": url,
        "retrieved_at": "2026-11-11T10:00:00Z",
        "content_sha256": "a" * 64,
        "exact_entity": True,
        "identity_proof": [{"type": "test_exact_match", "value": "x"}],
        "evidence_span": "Test span",
    }


def base_profile(org: str, name: str, with_financials: bool = True) -> dict:
    profile = {
        "organisation_number": org,
        "name": name,
        "legal_form": "AS",
        "municipality": "OSLO",
        "employees": 12,
        "industry_code": "62.010",
        "industry_label": "Programmeringstjenester",
        "website": "https://example.no/",
        "evidence": {
            "registry": {
                "field": "registry",
                "status": "available",
                "source_type": "official_registry_bulk",
                "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                "retrieved_at": "2026-11-11T10:00:00Z",
                "content_sha256": "b" * 64,
            },
            "website": {
                "field": "website",
                "status": "available",
                "source_type": "registry_website",
                "source_url": "https://example.no/",
                "retrieved_at": "2026-11-11T10:00:00Z",
                "content_sha256": "c" * 64,
                "value": {
                    "identity_assessment": {"publishable": True, "score": 0.99},
                    "description": "We build software for Norwegian companies.",
                },
            },
        },
    }
    if with_financials:
        profile["evidence"]["financials"] = {
            "field": "financials",
            "status": "available",
            "source_type": "official_annual_accounts",
            "source_url": f"https://data.brreg.no/regnskapsregisteret/regnskap/{org}",
            "retrieved_at": "2026-11-11T10:00:00Z",
            "content_sha256": "d" * 64,
            "value": {
                "records": [
                    {"period": "2024", "revenue": 2_000_000, "annual_result": 200_000},
                    {"period": "2023", "revenue": 1_000_000, "annual_result": 100_000},
                ]
            },
        }
    else:
        profile["evidence"]["financials"] = {
            "field": "financials",
            "status": "not_found",
            "source_type": "official_annual_accounts",
            "source_url": f"https://data.brreg.no/regnskapsregisteret/regnskap/{org}",
            "retrieved_at": "2026-11-11T10:00:00Z",
            "content_sha256": "d" * 64,
            "value": {"records": []},
        }
    return profile


def base_envelope(org: str) -> dict:
    return {
        "run_id": "test-001",
        "organisation_number": org,
        "state": "complete",
        "started_at": "2026-11-11T10:00:00Z",
        "completed_at": "2026-11-11T10:01:00Z",
        "modules": {
            "registry": {"state": "complete", "retry_count": 0, "final_timestamp": "2026-11-11T10:00:00Z"},
            "website": {"state": "complete", "retry_count": 0, "final_timestamp": "2026-11-11T10:00:00Z"},
        },
        "profile": {},
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class TestObservationValidation:
    def test_valid_observation_accepted(self):
        assert MERGE_MOD.validate_observation(good_obs("123456789", "news", "public_mention", "https://nrk.no/x")) == []

    def test_bad_hash_rejected(self):
        obs = good_obs("123456789", "news", "public_mention", "https://nrk.no/x")
        obs["content_sha256"] = "not-a-hash"
        assert "missing content hash" in MERGE_MOD.validate_observation(obs)

    def test_missing_proof_rejected(self):
        obs = good_obs("123456789", "news", "public_mention", "https://nrk.no/x")
        obs["identity_proof"] = []
        assert "missing exact-entity proof" in MERGE_MOD.validate_observation(obs)

    def test_unverified_entity_rejected(self):
        obs = good_obs("123456789", "news", "public_mention", "https://nrk.no/x")
        obs["exact_entity"] = False
        assert "exact legal entity is not verified" in MERGE_MOD.validate_observation(obs)


class TestSynthesis:
    def test_synthesis_is_deterministic(self):
        profile = base_profile("123456789", "Test AS")
        first = MERGE_MOD.build_synthesis(profile, {})
        second = MERGE_MOD.build_synthesis(profile, {})
        assert first == second

    def test_synthesis_reports_financial_change(self):
        profile = base_profile("123456789", "Test AS")
        out = MERGE_MOD.build_synthesis(profile, {})
        change = next(s for s in out["sections"] if s["heading"].startswith("What changed"))
        assert "+100.0%" in change["text"]  # revenue 1M -> 2M
        assert "2,000,000" in change["text"]

    def test_synthesis_states_unknowns_without_fabrication(self):
        profile = base_profile("123456789", "Test AS", with_financials=False)
        out = MERGE_MOD.build_synthesis(profile, {})
        change = next(s for s in out["sections"] if s["heading"].startswith("What changed"))
        assert "No normalized annual-account record was returned" in change["text"]
        unknown = next(s for s in out["sections"] if s["heading"] == "What is not known")
        assert "Not established by this run" in unknown["text"]
        assert "no annual accounts returned" in unknown["text"]

    def test_synthesis_uses_website_description_only_when_publishable(self):
        profile = base_profile("123456789", "Test AS")
        profile["evidence"]["website"]["value"]["identity_assessment"]["publishable"] = False
        out = MERGE_MOD.build_synthesis(profile, {})
        web = next(s for s in out["sections"] if s["heading"] == "Website description")
        assert "not established" in web["text"]
        assert "We build software" not in web["text"]


class TestMergeEndToEnd:
    def test_merge_preserves_every_envelope_and_validates(self, tmp_path):
        orgs = ["111111111", "222222222", "333333333"]
        profiles = [base_profile(o, f"Company {o} AS") for o in orgs]
        envelopes = [base_envelope(o) for o in orgs]
        # Attach profiles into envelopes the way run_competition_batch does.
        for env, prof in zip(envelopes, profiles):
            env["profile"] = prof
        write_jsonl(tmp_path / "profiles.jsonl", profiles)
        write_jsonl(tmp_path / "envelopes.jsonl", envelopes)
        news = [good_obs(orgs[0], "news", "public_mention", "https://nrk.no/article")]
        bad = good_obs(orgs[1], "news", "public_mention", "https://nrk.no/bad")
        bad["content_sha256"] = "xyz"
        write_jsonl(tmp_path / "obs-news.jsonl", news + [bad])
        for name in ("obs-reviews.jsonl", "obs-linkedin-handles.jsonl", "obs-linkedin-jobs.jsonl", "obs-youtube.jsonl"):
            (tmp_path / name).write_text("", encoding="utf-8")
        (tmp_path / "report-news.json").write_text(
            json.dumps({"company_results": [{"organisation_number": orgs[0]}, {"organisation_number": orgs[1]}, {"organisation_number": orgs[2]}]}),
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, str(MERGE), "--profiles", str(tmp_path / "profiles.jsonl"),
             "--envelopes", str(tmp_path / "envelopes.jsonl"),
             "--news", str(tmp_path / "obs-news.jsonl"),
             "--reviews", str(tmp_path / "obs-reviews.jsonl"),
             "--linkedin-handles", str(tmp_path / "obs-linkedin-handles.jsonl"),
             "--linkedin-jobs", str(tmp_path / "obs-linkedin-jobs.jsonl"),
             "--youtube", str(tmp_path / "obs-youtube.jsonl"),
             "--news-report", str(tmp_path / "report-news.json"),
             "--output", str(tmp_path / "final-envelopes.jsonl"),
             "--profiles-output", str(tmp_path / "final-profiles.jsonl"),
             "--report", str(tmp_path / "run-report.json"),
             "--run-id", "test-merge-001", "--expected-count", "3"],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        merged = [json.loads(line) for line in (tmp_path / "final-envelopes.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(merged) == 3
        assert [m["organisation_number"] for m in merged] == orgs
        assert all(m["state"] == "complete" for m in merged)
        merged_profiles = [json.loads(line) for line in (tmp_path / "final-profiles.jsonl").read_text(encoding="utf-8").splitlines()]
        first = merged_profiles[0]
        assert first["evidence"]["news"]["status"] == "available"
        assert first["evidence"]["synthesis"]["status"] == "available"
        assert len(first["external_observations"]) == 1
        second = merged_profiles[1]
        # Bad-hash observation was rejected; news checked but absent.
        assert second["evidence"]["news"]["status"] == "not_found"
        assert second["external_observations"] == []
        # LinkedIn jobs: no handle anywhere -> not_applicable, never a drop.
        assert all(p["evidence"]["linkedin_jobs"]["status"] == "not_applicable" for p in merged_profiles)
        # Brave: no key configured -> not_applicable.
        assert all(p["evidence"]["brave_discovery"]["status"] == "not_applicable" for p in merged_profiles)
        report = json.loads((tmp_path / "run-report.json").read_text(encoding="utf-8"))
        assert report["validation"]["passed"] is True
        assert report["observations"]["accepted"] == 1
        assert report["observations"]["rejected"] == 1
        assert report["operations"]["third_party_cost_usd"] == 0

    def test_merge_is_deterministic(self, tmp_path):
        orgs = ["111111111"]
        profiles = [base_profile(orgs[0], "Company AS")]
        envelopes = [base_envelope(orgs[0])]
        envelopes[0]["profile"] = profiles[0]
        write_jsonl(tmp_path / "profiles.jsonl", profiles)
        write_jsonl(tmp_path / "envelopes.jsonl", envelopes)
        for name in ("obs-news.jsonl", "obs-reviews.jsonl", "obs-linkedin-handles.jsonl", "obs-linkedin-jobs.jsonl", "obs-youtube.jsonl"):
            (tmp_path / name).write_text("", encoding="utf-8")

        def run_once(tag: str) -> str:
            result = subprocess.run(
                [sys.executable, str(MERGE), "--profiles", str(tmp_path / "profiles.jsonl"),
                 "--envelopes", str(tmp_path / "envelopes.jsonl"),
                 "--news", str(tmp_path / "obs-news.jsonl"),
                 "--reviews", str(tmp_path / "obs-reviews.jsonl"),
                 "--linkedin-handles", str(tmp_path / "obs-linkedin-handles.jsonl"),
                 "--linkedin-jobs", str(tmp_path / "obs-linkedin-jobs.jsonl"),
                 "--youtube", str(tmp_path / "obs-youtube.jsonl"),
                 "--output", str(tmp_path / f"final-{tag}.jsonl"),
                 "--profiles-output", str(tmp_path / f"profiles-{tag}.jsonl"),
                 "--report", str(tmp_path / f"report-{tag}.json"),
                 "--run-id", "test-det-001", "--expected-count", "1"],
                capture_output=True, text=True, cwd=ROOT,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            rows = [json.loads(line) for line in (tmp_path / f"profiles-{tag}.jsonl").read_text(encoding="utf-8").splitlines()]
            return rows[0]["evidence"]["synthesis"]["value"]["summary"]

        assert run_once("a") == run_once("b")


# ---------------------------------------------------------------------------
# Connector fixes verified against the smoke-001 run (11 Nov 2026):
#  - Fagfolkguiden HTTP 404 is "no directory page" (not_found), not a fetch
#    failure (source_error). Smoke-001 recorded 89 source_error for 404s.
#  - LinkedIn typeahead HTTP 429 is retried with backoff instead of failing
#    the company. Smoke-001 lost 10/100 companies to 429s.
# ---------------------------------------------------------------------------

import urllib.error


def _load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts"))
    sys.path.insert(0, str(ROOT / "src"))
    spec.loader.exec_module(module)
    return module


class TestReviewsConnector404:
    def test_404_is_no_page_not_an_error(self, tmp_path, monkeypatch):
        reviews = _load_script_module(
            "run_fagfolkguiden_reviews_connector",
            ROOT / "scripts" / "run_fagfolkguiden_reviews_connector.py",
        )

        def boom(request, timeout=25):
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", hdrs=None, fp=None
            )

        monkeypatch.setattr(reviews.urllib.request, "urlopen", boom)
        rows, status = reviews.fetch(
            {"organisation_number": "123456789", "name": "Test AS"},
            tmp_path / "cache",
        )
        assert rows == []
        assert status["accepted"] is False
        assert "error" not in status  # merge maps this to not_found
        assert status.get("reason") == "no_directory_page"

    def test_500_stays_an_error(self, tmp_path, monkeypatch):
        reviews = _load_script_module(
            "run_fagfolkguiden_reviews_connector",
            ROOT / "scripts" / "run_fagfolkguiden_reviews_connector.py",
        )

        def boom(request, timeout=25):
            raise urllib.error.HTTPError(
                request.full_url, 500, "Server Error", hdrs=None, fp=None
            )

        monkeypatch.setattr(reviews.urllib.request, "urlopen", boom)
        rows, status = reviews.fetch(
            {"organisation_number": "123456789", "name": "Test AS"},
            tmp_path / "cache",
        )
        assert rows == []
        assert "error" in status


class TestLinkedin429Retry:
    class _FlakyCache:
        def __init__(self, failures: int):
            self.failures = failures
            self.calls = 0

        def get(self, url, timeout, *, jobs=False):
            self.calls += 1
            if self.calls <= self.failures:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", hdrs=None, fp=None)
            return b"[]", "deadbeef", "snapshot"

    def test_retry_succeeds_after_two_429s(self):
        discovery = _load_script_module(
            "discover_linkedin_company_profiles",
            ROOT / "scripts" / "discover_linkedin_company_profiles.py",
        )
        cache = self._FlakyCache(failures=2)
        raw, digest, path = discovery.get_with_429_retry(
            cache, "https://example.invalid/x", 5, jobs=True, base_delay=0.01
        )
        assert raw == b"[]"
        assert cache.calls == 3

    def test_retry_gives_up_after_three_429s(self):
        discovery = _load_script_module(
            "discover_linkedin_company_profiles",
            ROOT / "scripts" / "discover_linkedin_company_profiles.py",
        )
        cache = self._FlakyCache(failures=99)
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            discovery.get_with_429_retry(
                cache, "https://example.invalid/x", 5, jobs=True, base_delay=0.01
            )
        assert excinfo.value.code == 429
        assert cache.calls == 3

    def test_non_429_error_is_not_retried(self):
        discovery = _load_script_module(
            "discover_linkedin_company_profiles",
            ROOT / "scripts" / "discover_linkedin_company_profiles.py",
        )

        class _NotFoundCache:
            def __init__(self):
                self.calls = 0

            def get(self, url, timeout, *, jobs=False):
                self.calls += 1
                raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

        cache = _NotFoundCache()
        with pytest.raises(urllib.error.HTTPError):
            discovery.get_with_429_retry(cache, "https://example.invalid/x", 5, base_delay=0.01)
        assert cache.calls == 1


# ---------------------------------------------------------------------------
# Bulk-snapshot gap (found by the 300-company scale test, 11 Nov 2026):
# 3/300 orgs exist in the live registry (API 200) but are absent from the
# frozen bulk CSV. The kit used to raise ValueError and kill the whole run;
# the agent now emits a stub profile with honest not_found registry evidence
# so the company still gets a terminal envelope via the live modules.
# ---------------------------------------------------------------------------

import csv
import gzip


def _load_batch_module():
    spec = importlib.util.spec_from_file_location(
        "norway_company_agent.batch", ROOT / "src" / "norway_company_agent" / "batch.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "src"))
    spec.loader.exec_module(module)
    return module


class TestBulkSnapshotGap:
    def _write_bulk(self, tmp_path, rows):
        path = tmp_path / "bulk.csv"
        fieldnames = [
            "organisasjonsnummer", "navn", "organisasjonsform.kode",
            "naeringskode1.kode", "naeringskode1.beskrivelse",
            "forretningsadresse.kommune", "forretningsadresse.kommunenummer",
            "hjemmeside", "sisteInnsendteAarsregnskap", "konkurs", "underAvvikling",
        ]
        with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fieldnames})
        return path

    def test_missing_org_gets_stub_not_crash(self, tmp_path):
        batch = _load_batch_module()
        bulk = self._write_bulk(tmp_path, [{
            "organisasjonsnummer": "111111111", "navn": "Present AS",
            "organisasjonsform.kode": "AS", "naeringskode1.kode": "62.010",
            "naeringskode1.beskrivelse": "Programmering",
            "forretningsadresse.kommune": "OSLO",
            "forretningsadresse.kommunenummer": "0301",
            "sisteInnsendteAarsregnskap": "2024",
        }])
        profiles, meta = batch.profiles_from_bulk(bulk, ["111111111", "222222222"])
        assert [p["organisation_number"] for p in profiles] == ["111111111", "222222222"]
        assert meta["requested"] == 2
        assert meta["selected"] == 2
        assert meta["absent_from_snapshot"] == ["222222222"]
        stub = profiles[1]
        assert stub["absent_from_registry_snapshot"] is True
        assert stub["evidence"]["registry"]["status"] == "not_found"
        assert "absent from the frozen registry snapshot" in stub["evidence"]["registry"]["note"]
        assert stub["evidence"]["accounting_obligation"]["status"] == "available"
        present = profiles[0]
        assert present["evidence"]["registry"]["status"] == "available"
        assert "absent_from_registry_snapshot" not in present

    def test_read_inputs_preserves_descriptive_fields(self, tmp_path):
        batch = _load_batch_module()
        inputs = tmp_path / "in.jsonl"
        inputs.write_text(json.dumps({
            "organisation_number": "222222222", "name": "Missing AS",
            "legal_form": "AS", "municipality": "BERGEN",
            "industry_code": "62.010", "website": "www.missing.no",
            "latest_submitted_accounts": "2024",
        }) + "\n", encoding="utf-8")
        records = batch.read_organisation_inputs(inputs)
        assert records[0]["name"] == "Missing AS"
        assert records[0]["legal_form"] == "AS"
        assert records[0]["municipality"] == "BERGEN"
        assert records[0]["website"] == "www.missing.no"
        assert records[0]["latest_submitted_accounts"] == "2024"


class TestLinkedinJobs429Retry:
    def test_frozen_fetch_retries_429(self, tmp_path, monkeypatch):
        jobs = _load_script_module(
            "run_linkedin_guest_jobs_connector",
            ROOT / "scripts" / "run_linkedin_guest_jobs_connector.py",
        )
        calls = {"n": 0}

        def flaky(url, timeout):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", hdrs=None, fp=None)
            return b"<html></html>"

        monkeypatch.setattr(jobs, "fetch", flaky)
        raw, digest, path = jobs.frozen_fetch(
            "https://example.invalid/jobs", tmp_path / "cache", 5, base_delay=0.01
        )
        assert raw == b"<html></html>"
        assert calls["n"] == 3
        assert Path(path).exists()

    def test_frozen_fetch_gives_up_after_three_429s(self, tmp_path, monkeypatch):
        jobs = _load_script_module(
            "run_linkedin_guest_jobs_connector",
            ROOT / "scripts" / "run_linkedin_guest_jobs_connector.py",
        )
        calls = {"n": 0}

        def always_429(url, timeout):
            calls["n"] += 1
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", hdrs=None, fp=None)

        monkeypatch.setattr(jobs, "fetch", always_429)
        with pytest.raises(urllib.error.HTTPError):
            jobs.frozen_fetch(
                "https://example.invalid/jobs", tmp_path / "cache", 5, base_delay=0.01
            )
        assert calls["n"] == 3

    def test_frozen_fetch_does_not_retry_404(self, tmp_path, monkeypatch):
        jobs = _load_script_module(
            "run_linkedin_guest_jobs_connector",
            ROOT / "scripts" / "run_linkedin_guest_jobs_connector.py",
        )
        calls = {"n": 0}

        def not_found(url, timeout):
            calls["n"] += 1
            raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

        monkeypatch.setattr(jobs, "fetch", not_found)
        with pytest.raises(urllib.error.HTTPError):
            jobs.frozen_fetch(
                "https://example.invalid/jobs", tmp_path / "cache", 5, base_delay=0.01
            )
        assert calls["n"] == 1
