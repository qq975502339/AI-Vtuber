from __future__ import annotations

from person_sidecar.tests.test_app import make_client


def test_unknown_persona_returns_not_found() -> None:
    with make_client() as client:
        response = client.post("/personas/missing/activate")

    assert response.status_code == 404
    assert response.json()["detail"] == "unknown persona id: missing"
