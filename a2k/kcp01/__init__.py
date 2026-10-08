"""A2K-KCP 0.1, the contract K2 speaks (sgt-ap11702-a2k reference packages).

An adapter at the edge, not a rewrite: an incoming 0.1 `ask`/`search` request
is mapped onto the internal A2KRequest, the engine (agent, vendors, cache)
runs unchanged, and its internal envelope is mapped back onto the 0.1 success
or failure envelope. The wire models and their validators are the reference
package's own (a2k_kcp / a2k_kbcard), so a response that breaks a 0.1 rule
fails here, not at K2.

Towards K2 this box is one KB, described by one card (cards/kcp01/
service_card.json); the vendors behind it are internal and never named.
"""
