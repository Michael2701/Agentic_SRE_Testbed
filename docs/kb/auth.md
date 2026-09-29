# Auth

Fake by design: real auth is a Stage 0 non-goal.

- Users are static: env `DEMO_USERS="alice:alice,bob:bob"` is parsed in `services/auth/app/config.py`,
  and passwords are compared with `secrets.compare_digest`.
- `POST /login` → opaque `secrets.token_urlsafe(32)` stored in Redis as `token:<t>` → username,
  TTL `TOKEN_TTL_SECONDS` (3600). Returns `{access_token, token_type: "bearer", expires_in}`, or 401.
- `POST /validate` with an `Authorization: Bearer <t>` header → `{user_id}` or 401.
- Gateway `authenticate()` (`services/gateway/app/main.py`) calls `/validate` for `/orders*`, then
  passes the identity downstream as `X-User-Id`. No header → 401 without calling auth.
- Redis is on the critical path of every `/orders` request, which makes it a natural fault target later.
