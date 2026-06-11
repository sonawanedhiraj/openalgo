# `test/upstream/` — tests adopted from upstream marketcalls/openalgo

Tests pulled from the upstream project on 2026-06-11 because they cover
production code our fork inherits but had no test for. Kept under this subpath so
their origin is unambiguous. Full audit + triage rationale:
[`outputs/2026-06-11_upstream_e2e_test_audit.md`](../../outputs/2026-06-11_upstream_e2e_test_audit.md).

Adopted tests are hermetic (`monkeypatch`-ed boundaries — no live broker, no
network, no real DB) and marked `@pytest.mark.unit`.

| File | Guards | Why adopted |
| --- | --- | --- |
| `test_mode_normalization.py` | WS LTP/Quote/Depth mode normalization (`websocket_proxy/mode_utils.py`, upstream issue #1375) | a zero-dep primitive our scanner/ZMQ path depends on; matches upstream exactly (34/34 green) |

## Not adopted (see the audit doc)

Three other upstream-only tests (`test_auth_resume.py`, `test_session_expiry.py`,
`test_multi_option_greeks_regression.py`) were evaluated but **run red against our
fork** — our implementations have diverged from upstream in those paths. They are
**ADAPT** follow-ups: port the upstream production behavior first, then adopt the
test. `test_dhan_margin_api.py` is **SKIP** (covers a broker we don't trade). The
audit doc records each divergence (the auth-resume one is a plausible latent bug).
