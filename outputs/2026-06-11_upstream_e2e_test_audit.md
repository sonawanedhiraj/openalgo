# Upstream OpenAlgo e2e/test audit — 2026-06-11

**Question:** Are upstream `marketcalls/openalgo` tests worth pulling into our fork?

**Method:** `git fetch upstream main --no-tags --depth=1`, diffed the upstream
`test/` tree against ours by filename, then read every file the upstream has and
we don't, and verified each test's production target exists in *our* fork with a
matching signature.

## Headline

Our fork was cut from upstream, so **~58 of upstream's test files we already
have verbatim by name** (`test_broker.py`, `test_websocket.py`, the whole
`test/sandbox/` dir, etc.). They are not "adopt" candidates — they're our own
inheritance.

Upstream has exactly **5 test files we do NOT have**:

| Upstream file | Targets (prod code) | Exists in our fork? | Hermetic? | Ran green vs our code? | Verdict |
| --- | --- | --- | --- | --- | --- |
| `test_mode_normalization.py` | `websocket_proxy/mode_utils.py::normalize_mode` | ✅ both fns present | ✅ pure function, zero deps | ✅ **34/34 pass** | **ADOPT** |
| `test_auth_resume.py` | `blueprints/auth.py::_try_resume_broker_session`, `login()` | ✅ `auth.py:137` (used at :272, :370) | ✅ monkeypatch, no broker/net | ❌ 4/6 fail — **fork diverged** | **ADAPT** |
| `test_session_expiry.py` | `utils/session.py::revoke_user_tokens` | ✅ `session.py:97` | ✅ monkeypatch, fake socketio | ❌ fail — **fork diverged** | **ADAPT** |
| `test_multi_option_greeks_regression.py` | `services/option_greeks_service.py` | ✅ both fns present | ✅ monkeypatch, no net | ❌ 2/2 fail — **fork diverged** | **ADAPT** |
| `test_dhan_margin_api.py` | `broker/dhan/api/margin_api.py`, `mapping/margin_data.py` | ✅ files present | ✅ monkeypatch | (not run — broker we don't trade) | **SKIP** |

**Tally: 1 ADOPT, 3 ADAPT, 1 SKIP (of the genuinely-new files); ~58 SKIP (already inherited).**

All 5 new files are hermetic by construction (every external boundary is
`monkeypatch`-ed — no live broker, no network, no real DB). None expect a running
server. So `@pytest.mark.live` exclusion is not needed for any of them.

> **Why the verdicts changed after running them.** The initial paper triage rated
> 4 of these ADOPT because the target functions all exist in our fork. Actually
> *running* them against our code (the right move — signatures existing ≠ behavior
> matching) showed 3 of the 4 are **red against our fork**: our fork has diverged
> from upstream in those exact code paths. Adopting a red test breaks the suite,
> and porting the upstream production fix is beyond a test-adoption task — so they
> drop to ADAPT, and the divergences are reported below as findings (the real value
> of this audit).

## ADOPT — value rationale

1. **`test_mode_normalization.py`** (34 parametrized cases, all green) — Pure-function
   coverage of WS subscription mode normalization (LTP/Quote/Depth, upstream issue
   #1375 silent-failure). Our `websocket_proxy/mode_utils.py` matches upstream
   exactly. Zero-dependency, zero-maintenance, and we touch the WebSocket/ZMQ layer
   heavily (scanner self-subscribe, pre-subscribe boot race) — cheap insurance on a
   primitive our scanner path depends on.

## Divergence findings — the 3 ADAPT tests (what running them surfaced)

These don't pass as-is because **our fork's implementation differs from upstream's
current behavior**. Each is a real, separately-actionable finding:

1. **`_try_resume_broker_session` (auth resume) — robustness gap, plausible latent bug.**
   Upstream validates a stored broker token via a `test_auth_token(token) ->
   (ok, msg)` call *plus* treats a structured `{"status":"error",...}` funds payload
   as invalid. **Our fork** (`blueprints/auth.py:162-173`) validates *only* with
   `get_margin_data(token)` and the test `if not funds_data:`. A structured error
   dict like `{"status":"error","message":"token expired"}` is **non-empty →
   truthy**, so our fork would treat an expired-token error payload as *valid* and
   **resume the session**. For Zerodha (which returns `{}` on failure) this is
   masked, but it is a genuine robustness gap on the exact "Morning 401 = stale
   broker session" surface we hit daily. **Worth a follow-up bug fix**, then adopt
   the test alongside it. (The 2 `login()` cases also fail with `AttributeError` —
   upstream restructured `login()` with module-level helpers
   `find_user_by_exact_username` / `is_session_valid` our `auth.py` doesn't expose;
   that part is upstream-shape-specific, lower interest.)

2. **`revoke_user_tokens` (session expiry) — missing upstream feature.**
   Upstream's 3 AM auto-expiry **broadcasts** `force_logout` + `active_sessions_update`
   over SocketIO so other logged-in devices are kicked immediately. **Our fork**
   (`utils/session.py:97-185`) revokes tokens and clears caches/sessions but emits
   **no SocketIO events**. Not a bug — a feature we never pulled. Low urgency (single
   self-hosted operator, rarely multi-device), but cheap to port if multi-device
   force-logout is ever wanted.

3. **`get_multi_option_greeks` / `calculate_time_to_expiry` (option greeks) — graceful expired-leg handling we lack.**
   Upstream floors a same-day naive expiry to `0.0` years/days (treating naive as
   IST) and converts an expired leg to a zero-greeks `success` row inside a multi-leg
   response. **Our fork** returns a small positive `years_to_expiry`
   (`0.000599…` vs `0.0`) for the same input and propagates the per-leg error instead
   of degrading to zero-greeks. Behavioral divergence in the greeks service; relevant
   since we run option strategies. Port the upstream handling first, then adopt.

## SKIP — rationale

- **`test_dhan_margin_api.py`** — Hermetic and would pass (the Dhan margin code
  exists and `dhan` is in `VALID_BROKERS`), but our fork trades **Zerodha**; we
  never modify Dhan broker code, so this carries no regression value for our
  change surface — only maintenance burden on a broker we don't use. Honest call:
  no value, skip.

- **The ~58 name-matching files** — already in our `test/` (we forked from
  upstream). Not adoption candidates. Where upstream has *newer* content for a
  shared filename, that's a future `git diff upstream/main -- test/<file>` sweep,
  out of scope for "adopt tests we lack."

## Integration notes (Phase 2 — what actually landed)

- **Adopted: 1 file → `test/upstream/test_mode_normalization.py`** (34 parametrized
  cases, all green). Plus `test/upstream/__init__.py` and `README.md`.
- Landing under `test/upstream/` so origin is obvious. The global
  `test/conftest.py` DB-isolation guard (commit `fcf96b91`) covers any subdir, so
  adopted tests are structurally barred from the live `db/openalgo.db`.
- Minimal porting: upstream resolves its module path via
  `Path(__file__).resolve().parent.parent`; nested one level deeper in
  `test/upstream/` that became `parents[2]` — the only functional edit. Added
  `pytestmark = pytest.mark.unit`.
- RELIANCE phantom-row baseline in live `db/openalgo.db` before/after the run:
  **32 → 32** (conftest-guard verification — pytest could not touch the live DB).

## Follow-ups (out of scope tonight)

- **Fix the auth-resume robustness gap** (finding #1) — make
  `_try_resume_broker_session` reject structured `{"status":"error"}` funds
  payloads (and optionally add a `test_auth_token` path), then adopt
  `test_auth_resume.py`. This is the highest-value follow-up: it sits on the
  "Morning 401" surface.
- **(Optional) port multi-device force-logout** (finding #2), then adopt
  `test_session_expiry.py`.
- **(Optional) port graceful expired-leg greeks** (finding #3), then adopt
  `test_multi_option_greeks_regression.py`.
- A broader `git diff upstream/main -- test/` sweep on the ~58 *shared-name* files
  could surface upstream test improvements to our inherited files — separate effort.
