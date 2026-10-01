"""Cache lifecycle (Propuesta_cache_CdV) on Aurora + pgvector.

- retrieval.lookup(): is this question already answered? (Sheet 4)
- admission.submit(): judge a vendor answer and cache it if FIT, in the
  background (Sheet 3)

The gateway (a2k/gateway/engine.py) calls both according to CACHE_MODE --
off, shadow (the cache decides but the user always gets the vendor answer)
or serve. Everything else here is a step of one of the two, importable and
testable on its own.
"""
