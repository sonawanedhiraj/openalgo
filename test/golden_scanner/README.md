# Golden scanner incident replays

Every production scanner incident (a real false BUY/SELL, or a real signal
that should have fired and didn't) gets a permanent, replayable test case
here, built from `test/fixtures/frame_factory.py` production-shaped frames —
never the bare no-timestamp synthetic frames used by the original gate-logic
unit tests. A case is added in the same PR that fixes (or documents) the
underlying incident, named `test_golden_YYYY_MM_DD_<slug>.py`, and encodes
the DESIRED behavior explicitly, even when that behavior doesn't hold on
`dev` yet — mark it `@pytest.mark.xfail(reason="...", strict=False)` citing
the tracking issue, so it goes green (XPASS, non-fatal under `strict=False`)
the moment the real fix lands, and CI stays informative either way.
