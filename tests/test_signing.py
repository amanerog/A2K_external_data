from a2k_box.gateway import signing


def test_sign_and_verify_round_trip():
    envelope_dict = {
        "a2kVersion": "0.6-baseline",
        "operation": "ask",
        "sourceKbId": "urn:a2k:gateway:k2-external-intel",
        "answer": "Test answer.",
        "claims": [],
        "citations": [],
        "grounding": {"groundedRatio": 1.0},
        "freshness": None,
        "accessDecision": {"decision": "allowed"},
        "audit": {"logged": True},
    }
    sig = signing.sign_envelope(envelope_dict)
    assert sig["alg"] == "EdDSA"
    assert signing.verify_envelope(envelope_dict, sig["jws"]) is True


def test_verify_fails_on_tampered_payload():
    envelope_dict = {
        "a2kVersion": "0.6-baseline",
        "operation": "ask",
        "sourceKbId": "urn:a2k:gateway:k2-external-intel",
        "answer": "Original answer.",
        "claims": [],
        "citations": [],
        "grounding": {},
        "freshness": None,
        "accessDecision": {},
        "audit": {},
    }
    sig = signing.sign_envelope(envelope_dict)

    tampered = dict(envelope_dict)
    tampered["answer"] = "Tampered answer."
    assert signing.verify_envelope(tampered, sig["jws"]) is False


def test_jwks_exposes_the_signing_public_key():
    keys = signing.jwks()["keys"]
    assert len(keys) == 1
    assert keys[0]["kty"] == "OKP"
    assert keys[0]["crv"] == "Ed25519"
