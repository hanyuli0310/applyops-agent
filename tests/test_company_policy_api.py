from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from applyops.api.app import create_app
from applyops.company_policy import CompanyPolicy


def _client() -> tuple[TestClient, Path, object]:
    root = Path(tempfile.mkdtemp(prefix="applyops-company-api-"))
    app = create_app(root, frontend_dist=None, headless=True)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers.update({"X-ApplyOps-Token": app.state.applyops.session_token})
    return client, root, app


def test_company_policy_api_exposes_defaults_and_saves_lists():
    client, _root, _app = _client()
    with client:
        initial = client.get("/api/company-policy")
        assert initial.status_code == 200
        assert initial.json()["default_policy"] == "auto"
        assert "Google" in initial.json()["review_companies"]

        saved = client.post(
            "/api/company-policy",
            json={
                "default_policy": "auto",
                "review_companies": ["Google", "NVIDIA"],
                "never_companies": ["Never Co"],
            },
        )
        assert saved.status_code == 200
        assert saved.json()["review_companies"] == ["Google", "NVIDIA"]

        reread = client.get("/api/company-policy").json()
        assert reread["never_companies"] == ["Never Co"]


def test_runner_status_includes_company_policy_and_review_attention_count():
    client, _root, _app = _client()
    with client:
        status = client.get("/api/runner/status")
        assert status.status_code == 200
        body = status.json()
        assert body["company_policy"]["default_policy"] == "auto"
        assert body["review_waiting"] == []
        assert body["policy_usable"] is True


def test_review_queue_can_skip_and_allow_future_company_auto_delivery():
    client, _root, app = _client()
    state = app.state.applyops
    row = state.service.enqueue(
        job_url="https://example.test/google",
        job_id="google-1",
        route="demo",
        platform="DemoATS",
        title="Backend Engineer",
        company="Google LLC",
    )
    request = state.authorizer.create_request(
        job_key=row.job_key,
        job_url=row.job_url,
        route=row.route,
        platform=row.platform,
        fields={"name": "Jane Doe"},
        application_id=row.id,
    )
    with client:
        allowed = client.post(f"/api/requests/{request.request_id}/allow-company")
        assert allowed.status_code == 200
        assert "Google" not in allowed.json()["policy"]["review_companies"]

        # A second review item can be skipped and lands in the durable ledger.
        state.company_policy.set(CompanyPolicy(review_companies=["Google"]))
        row2 = state.service.enqueue(
            job_url="https://example.test/google-2",
            job_id="google-2",
            route="demo",
            platform="DemoATS",
            title="Backend Engineer",
            company="Google",
        )
        request2 = state.authorizer.create_request(
            job_key=row2.job_key,
            job_url=row2.job_url,
            route=row2.route,
            platform=row2.platform,
            fields={"name": "Jane Doe"},
            application_id=row2.id,
        )
        skipped = client.post(f"/api/requests/{request2.request_id}/skip")
        assert skipped.status_code == 200
        assert state.service.get(row2.id).state == "skipped"
