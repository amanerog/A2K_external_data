import pytest

from a2k.gateway.engine import GatewayEngine
from a2k.models.request import A2KRequest, GetDocumentRequest, Pagination


@pytest.fixture
def engine():
    return GatewayEngine()


async def test_search_returns_passages_not_answer(engine):
    req = A2KRequest(operation="search", query="Acme Robotics")
    envelope = await engine.search(req)

    assert envelope.ok is True
    assert envelope.answer is None
    assert len(envelope.passages) > 0
    assert len(envelope.citations) == len(envelope.passages)
    assert envelope.pageInfo.pageLimit == 10


async def test_search_respects_pagination_limit(engine):
    req = A2KRequest(operation="search", query="Acme Robotics", pagination=Pagination(limit=2))
    envelope = await engine.search(req)

    assert len(envelope.passages) <= 2


async def test_get_document_round_trip_from_a_citation(engine):
    search_req = A2KRequest(operation="search", query="Nordic Cold Chain", sources=["cala"])
    search_envelope = await engine.search(search_req)
    document_id = search_envelope.citations[0].documentId

    doc_req = GetDocumentRequest(documentId=document_id)
    doc_response = await engine.get_document(doc_req)

    assert doc_response.ok is True
    assert doc_response.document.documentId == document_id
    assert "Nordic Cold Chain" in doc_response.document.content


async def test_get_document_unknown_id_is_not_found(engine):
    doc_req = GetDocumentRequest(documentId="urn:a2k:vendor:cala:doc:does-not-exist:profile")
    doc_response = await engine.get_document(doc_req)

    assert doc_response.ok is False
    assert doc_response.error.code == "NOT_FOUND"
