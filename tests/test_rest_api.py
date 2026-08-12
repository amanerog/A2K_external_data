from fastapi.testclient import TestClient

from a2k.api.rest import app

client = TestClient(app)


def test_well_known_card():
    response = client.get("/.well-known/a2k-card.json")
    assert response.status_code == 200
    assert response.json()["id"] == "urn:a2k:gateway:k2-external-intel"


def test_ask_returns_envelope_with_conflict():
    response = client.post("/a2k/ask", json={"query": "Meridian Textiles ownership"})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert len(data["conflicts"]) == 1
    # Level 2 (see README "Conformance"): no response signing.
    assert data["responseSignature"] is None


def test_single_source_route():
    response = client.post("/a2k/cala/search", json={"query": "Acme Robotics"})
    assert response.status_code == 200
    data = response.json()
    assert data["sourceKbId"] == "urn:a2k:vendor:cala"


def test_unsupported_operation_returns_400():
    response = client.post("/a2k/bogus", json={"query": "x"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNSUPPORTED_OPERATION"


def test_unknown_source_returns_404():
    response = client.post("/a2k/notasource/ask", json={"query": "x"})
    assert response.status_code == 404


def test_get_document_not_found_returns_404():
    response = client.post("/a2k/getDocument", json={"documentId": "urn:a2k:vendor:cala:doc:nope:profile"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_stream_ask_emits_text_chunks_and_proof_footer():
    with client.stream("POST", "/a2k/streamAsk", json={"query": "Acme Robotics"}) as response:
        body = "".join(response.iter_text())
    assert "event: text_chunk" in body
    assert "event: proof_footer" in body
