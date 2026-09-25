# Authentication

This page is for developers building clients that call **write** endpoints — currently the import endpoints under `/api/v2/import/`. Most API endpoints are read-only and do not require authentication; if that's all you need, skip this page.

Authentication uses the [**OAuth 2.1**](https://oauth.net/2.1/) authorization code grant with [**PKCE**](https://oauth.net/2/pkce/).

## Conformance

We implement the OAuth 2.1 draft plus the listed RFCs:

- Authorization code grant only — no implicit, password, or client-credentials grant.
- PKCE is **required for all clients** (RFC 7636), public and confidential alike.
- `code_challenge_method` **must be `S256`** — the `plain` method is rejected.
- Refresh tokens are rotated on every exchange; reuse triggers full token-chain revocation.
- Redirect URIs are matched as **exact strings** (no prefix or substring matching), with one exception: for loopback IP redirect URIs (`http://127.0.0.1/...`, `http://[::1]/...`), any port is allowed per RFC 8252 §7.3.
- Bearer tokens are sent in the `Authorization` header (RFC 6750). Form and query parameter token transport are not supported.
- Dynamic client registration is offered as a Mastodon-style subset of RFC 7591.

## Endpoints

| URL                    | Purpose                       | Spec                            |
| ---------------------- | ----------------------------- | ------------------------------- |
| `/api/v2/apps/`        | Dynamic client registration   | RFC 7591 (subset)               |
| `/oauth/authorize/`    | Authorization endpoint        | OAuth 2.1 §4.1.1                |
| `/oauth/token/`        | Token endpoint                | OAuth 2.1 §4.1.3, §4.3.1        |
| `/oauth/revoke_token/` | Token revocation              | RFC 7009                        |
| `/api/v2/auth/me/`     | Inspect the current token     | —                               |

## Client types

| Type           | Token-endpoint client auth | Use for                                          |
| -------------- | -------------------------- | ------------------------------------------------ |
| `public`       | None (PKCE only)           | Native apps, mobile apps, SPAs, CLIs             |
| `confidential` | HTTP Basic with secret     | Server-side web apps that can hold a secret      |

PKCE applies to both. Public clients **must not** ship a `client_secret`.

## Scopes

| Scope    | Grants                                                                         |
| -------- | ------------------------------------------------------------------------------ |
| `read`   | Every read endpoint is anonymous, so requesting this scope is superfluous.     |
| `import` | Permission to upload images via `/api/v2/import/...`. Additionally requires the authenticated user to have contributor (staff) status. |

The default scope when none is requested is `read`.
Additional scopes will be added in the future.

## Registering a client

See [Apps](apps.md) for the full registration reference.

```
POST /api/v2/apps/
```

You receive a `client_id` (and a `client_secret` if you registered as `confidential`).
Credentials are valid only on the instance that issued them.

## Authorization request

Send the user's browser to the authorization endpoint with the parameters below.

```
GET https://yesterdays.today/oauth/authorize/
    ?response_type=code
    &client_id={CLIENT_ID}
    &redirect_uri={REDIRECT_URI}
    &scope=read+import
    &state={STATE}
    &code_challenge={CHALLENGE}
    &code_challenge_method=S256
```

| Parameter                | Required | Notes                                                                         |
| ------------------------ | -------- | ----------------------------------------------------------------------------- |
| `response_type`          | yes      | Must be `code`.                                                                |
| `client_id`              | yes      | Issued at registration.                                                        |
| `redirect_uri`           | yes      | Exact match against a URI registered for this client.                          |
| `scope`                  | no       | Space-separated list of scopes. Defaults to `read`.                            |
| `state`                  | no       | Opaque value echoed back in the redirect. Strongly recommended even with PKCE. |
| `code_challenge`         | yes      | Base64url-encoded SHA-256 of the verifier (RFC 7636 §4.2).                     |
| `code_challenge_method`  | yes      | Must be `S256`.                                                                |

The user sees a consent screen showing your app's `name`, the requested scopes, and the redirect URI you provided. If they approve, the browser is redirected to your `redirect_uri` with a `code` and the `state` you sent.

Consent is remembered. The screen appears the first time your app requests authorization, and again only if you request a scope the user hasn't already granted (or if you pass `approval_prompt=force`). Otherwise, repeat authorization requests redirect back to you immediately — even after the user's tokens have expired or been revoked. Users can withdraw a remembered consent at any time from the **Authorized Applications** page in their account settings, which also revokes all of your app's tokens; the next authorization request then shows the consent screen again.

### Generating the PKCE verifier and challenge

The verifier is a 43–128 character random string from the unreserved-character set. The challenge is the base64url-encoded (no padding) SHA-256 hash of the verifier (RFC 7636 §4.1, §4.2).

=== "Python"

    ```python
    import base64
    import hashlib
    import secrets
    
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()  # (1)!
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )  # (2)!
    ```

    1. `secrets.token_bytes(32)` draws 32 cryptographically-random bytes; `urlsafe_b64encode` renders them as text in the URL-safe alphabet (`-`/`_` instead of `+`/`/`); `.rstrip(b"=")` strips the trailing `=` padding PKCE forbids; `.decode()` turns the `bytes` into a `str`. This is the secret you keep.
    2. The challenge is the verifier's SHA-256 hash, encoded the same way. In execution order:
        1. `hashlib.sha256(verifier.encode()).digest()` hashes the verifier's bytes and returns the raw digest (*not* the hex form).
        2. `base64.urlsafe_b64encode(…)` base64url-encodes that digest.
        3. `.rstrip(b"=")` strips the padding.
        4. `.decode()` turns the `bytes` into a `str`.

        The server reruns this exact sequence on the verifier you reveal later and compares, so any deviation (hex digest, leftover padding) breaks the match. The challenge is the only part of the secret that travels in the URL.

=== "R"

    ```r
    library(openssl)
    library(base64enc)

    b64url <- function(x) {
      enc <- base64enc::base64encode(x)
      gsub("=+$", "", chartr("+/", "-_", enc))  # (1)!
    }

    verifier <- b64url(openssl::rand_bytes(32))  # (2)!
    challenge <- b64url(openssl::sha256(charToRaw(verifier), raw = TRUE))  # (3)!
    ```

    1. base64url isn't built into base R, so this helper makes one: `chartr("+/", "-_", enc)` swaps standard base64's `+`/`/` for the URL-safe `-`/`_`, and `gsub("=+$", "", …)` strips trailing `=` padding.
    2. 32 cryptographically-random bytes, base64url-encoded — the secret you keep.
    3. The challenge is the SHA-256 of the verifier. `charToRaw` turns the verifier string into the bytes `sha256` expects, and `raw = TRUE` returns the raw digest (not a hex string) so the base64url encoding matches what the server recomputes. Hash first, then encode — order matters.

## Authorization response

On success, the browser is redirected to:

```
{REDIRECT_URI}?code={CODE}&state={STATE}
```

On failure, the redirect carries OAuth 2.1 §4.1.2.1 error parameters:

| `error`                     | Cause                                                               |
| --------------------------- | ------------------------------------------------------------------- |
| `access_denied`             | User clicked Cancel.                                                |
| `invalid_request`           | Required parameter missing or malformed.                            |
| `unauthorized_client`       | Client not permitted to use the authorization-code grant.           |
| `unsupported_response_type` | `response_type` was not `code`.                                     |
| `invalid_scope`             | A requested scope is unknown or not allowed for this client.        |
| `server_error`              | The authorization server hit an internal error.                     |

If the request omits `code_challenge_method=S256` entirely, the server responds with `400 Bad Request` directly (no redirect), since the redirect URI cannot yet be trusted.

### Receiving the redirect

How you receive `code` depends on your client type.

#### Native / CLI: loopback redirect (RFC 8252 §7.3)

Bind a one-shot HTTP server to `127.0.0.1` on an ephemeral port, register `http://127.0.0.1/callback` as a redirect URI, then open the user's browser.

!!! note "These are excerpts, not runnable programs"

    The snippets below show only the receiver. They assume the PKCE pair and `state` built earlier on this page.
    `auth_url` is the [`/oauth/authorize/`](#authorization-request) URL with your query parameters filled in, including `redirect_uri=http://127.0.0.1:{port}/callback`.

=== "Python"

    ```python
    import http.server
    import socket
    import urllib.parse

    # (1)!

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["code"] = qs.get("code", [None])[0]
            result["state"] = qs.get("state", [None])[0]
            result["error"] = qs.get("error", [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"You may close this tab.")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    print(f"Listening on http://127.0.0.1:{port}/callback")

    # (2)!
    
    server.handle_request()
    ```

    1. Here we build the PKCE pair, state, and authorization URL as shown above.
    2. Here we open the user's browser (perhaps using [`webbrowser`](https://docs.python.org/3/library/webbrowser.html)) to the authorization URL before handling the request.

=== "R"

    ```r
    library(httpuv)

    # (1)!

    result <- new.env()

    server <- startServer("127.0.0.1", 0, list(
      call = function(req) {
        qs <- parseQueryString(req$QUERY_STRING)
        result$code  <- qs$code
        result$state <- qs$state
        result$error <- qs$error
        list(status = 200L, body = "You may close this tab.")
      }
    ))
    port <- server$getPort()
    cat(sprintf("Listening on http://127.0.0.1:%d/callback\n", port))

    # (2)!
    while (is.null(result$code) && is.null(result$error)) {
      service()
      Sys.sleep(0.1)
    }
    stopServer(server)
    ```

    1. Here we build the PKCE pair, state, and authorization URL as shown above.
    2. Here we open the user's browser (perhaps using [`browseURL`](https://rdrr.io/r/utils/browseURL.html)) to the authorization URL before entering the wait loop.

See the [complete example](#complete-example) programs for the full flow with these steps filled in.

#### Server-side web app: HTTPS callback

Register a route in your framework matching the `redirect_uri` you registered. The handler reads `code`, `state`, and any `error` from the query string.

In all cases, **verify the `state`** value matches the one you generated before continuing.

## Token request

Exchange the authorization code at the token endpoint within 60 seconds.

```
POST https://yesterdays.today/oauth/token/
Content-Type: application/x-www-form-urlencoded
```

Parameters for the `authorization_code` grant:

| Parameter        | Required                | Notes                                                  |
| ---------------- | ----------------------- | ------------------------------------------------------ |
| `grant_type`     | yes                     | Must be `authorization_code`.                           |
| `code`           | yes                     | The code from the redirect.                             |
| `redirect_uri`   | yes                     | Must match the URI used at `/oauth/authorize/`.         |
| `client_id`      | yes (public)            | Required for public clients.                            |
| `code_verifier`  | yes                     | The verifier whose challenge you sent at `/authorize/`. |

**Confidential clients** authenticate using HTTP Basic per OAuth 2.1 §2.4.1 — `Authorization: Basic base64(client_id:client_secret)` — and omit `client_id` from the form body.

=== "curl"

    ```bash
    curl -X POST "https://yesterdays.today/oauth/token/" \
      -d grant_type=authorization_code \
      -d code="$CODE" \
      -d redirect_uri="http://127.0.0.1:$PORT/callback" \
      -d client_id="$CLIENT_ID" \
      -d code_verifier="$VERIFIER"
    ```

=== "Python"

    ```python
    import requests

    resp = requests.post(
        "https://yesterdays.today/oauth/token/",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"http://127.0.0.1:{port}/callback",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    tokens = resp.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.today/oauth/token/") |>
      req_body_form(
        grant_type    = "authorization_code",
        code          = code,
        redirect_uri  = sprintf("http://127.0.0.1:%d/callback", port),
        client_id     = client_id,
        code_verifier = verifier
      ) |>
      req_perform()
    tokens <- resp_body_json(resp)
    ```

### Refresh token grant

```
POST https://yesterdays.today/oauth/token/
Content-Type: application/x-www-form-urlencoded
```

| Parameter       | Required          | Notes                                  |
| --------------- | ----------------- | -------------------------------------- |
| `grant_type`    | yes               | Must be `refresh_token`.                |
| `refresh_token` | yes               | The current refresh token.              |
| `client_id`     | yes (public)      | Required for public clients.            |
| `scope`         | no                | If omitted, original scopes are kept.   |

Confidential client authentication is the same as for the authorization code grant.

## Token response

Success is `200 OK` with a JSON body (OAuth 2.1 §3.2.3):

```json
{
    "access_token": "abc123...",
    "expires_in": 28800,
    "token_type": "Bearer",
    "scope": "read import",
    "refresh_token": "def456..."
}
```

Every successful exchange — initial and refresh — issues a new `refresh_token`.
Replace any previously stored value.

Failure is `400 Bad Request` with an OAuth 2.1 §3.2.4 error body:

```json
{ "error": "invalid_grant", "error_description": "..." }
```

## Using access tokens

Send the access token as a Bearer credential per RFC 6750 §2.1:

```
Authorization: Bearer {ACCESS_TOKEN}
```

Inspect the current token:

=== "curl"

    ```bash
    curl "https://yesterdays.today/api/v2/auth/me/" \
      -H "Authorization: Bearer $ACCESS_TOKEN"
    ```

=== "Python"

    ```python
    import requests

    resp = requests.get(
        "https://yesterdays.today/api/v2/auth/me/",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    me = resp.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.today/api/v2/auth/me/") |>
      req_headers(Authorization = paste("Bearer", access_token)) |>
      req_perform()
    me <- resp_body_json(resp)
    ```

The response describes the token's user, scopes, and capabilities:

```json
{
    "osm_id": 123456,
    "username": "alice",
    "is_staff": true,
    "can_import": true,
    "scopes": ["read", "import"]
}
```

`can_import` reflects what *this token* can do (user has staff status **and** the token holds `import` scope), not what the user could do under a fully-scoped token.

## Refresh token rotation and reuse detection

Every refresh issues a new refresh token; the previous one is marked revoked. A revoked refresh token presented within a 30-second grace window still works, to absorb network-glitch retries on the client side.

Outside the grace window, presenting a revoked refresh token causes the **entire token chain to be revoked** — every refresh and access token issued from the same original consent grant. The legitimate user is logged out of the client and must re-authorize. Treat any unexpected re-auth prompt as a possible signal that the token was used by another party.

If your client receives `invalid_grant` on a refresh, do not retry with the same refresh token — start a fresh authorization request.

## Token lifetimes

| Artifact             | Lifetime    | Notes                              |
| -------------------- | ----------- | ---------------------------------- |
| Authorization code   | 60 seconds  | Single use; deleted on exchange.   |
| Access token         | 8 hours     | Bearer token for API calls.        |
| Refresh token        | 30 days     | Rotated on every successful use.   |

## Errors

| HTTP | `error`                          | Meaning                                                                  |
| ---- | -------------------------------- | ------------------------------------------------------------------------ |
| 400  | `invalid_request`                | Required parameter missing or malformed.                                  |
| 400  | `invalid_client`                 | `client_id` unknown, or client authentication failed.                     |
| 400  | `invalid_grant`                  | Code or refresh token expired, revoked, or doesn't match the client.      |
| 400  | `unauthorized_client`            | Client not permitted to use the requested grant.                          |
| 400  | `unsupported_grant_type`         | `grant_type` is not one we support.                                       |
| 400  | `invalid_scope`                  | A requested scope is unknown or not allowed for this client.              |
| 400  | (none)                           | `code_challenge_method` was missing or not `S256`.                        |
| 400  | `access_denied`                  | User clicked Cancel on the consent screen.                                |
| 401  | `invalid_token` (RFC 6750)       | Access token expired, revoked, or unrecognized.                           |
| 403  | `insufficient_scope` (RFC 6750)  | Token is valid but lacks a scope required by the endpoint.                |
| 429  | —                                | Rate limit exceeded. Inspect `Retry-After`.                               |
| 500  | `server_error`                   | Internal failure on our side.                                             |

## Rate limits

| Endpoint              | Limit         | Keyed by  |
| --------------------- | ------------- | --------- |
| `POST /api/v2/apps/`  | 1 / minute    | Client IP |
| `POST /oauth/token/`  | 150 / minute  | Client IP |

Exceeding a limit returns `429 Too Many Requests` with a `Retry-After` header.

## Security requirements for clients

- Use PKCE with `code_challenge_method=S256`. The `plain` method is rejected.
- Public clients **must not** embed a `client_secret` in distributed binaries or browser code.
- Native clients **must** use a loopback (`http://127.0.0.1:{port}`) redirect URI per RFC 8252 §7.3, or a claimed-`https` URI. Custom URI schemes are not currently allowed.
- Send and verify the `state` parameter on every authorization request.
- All non-loopback redirect URIs must use `https`.
- Do not retry a failed refresh by re-presenting the same refresh token; start a fresh authorization flow.
- When the user signs out of your app, revoke their tokens at `/oauth/revoke_token/` per RFC 7009.

## Complete example

=== "Python"

    ```python
    import base64
    import hashlib
    import http.server
    import secrets
    import socket
    import urllib.parse
    import webbrowser
    
    import requests
    
    INSTANCE = "https://yesterdays.today"
    CLIENT_ID = "your-public-client-id"
    
    # 1. PKCE
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()  # (1)!
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()  # (2)!
    state = secrets.token_urlsafe(16)  # (3)!
    
    # 2. Loopback callback
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))  # (4)!
    port = sock.getsockname()[1]  # (5)!
    sock.close()  # (6)!
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    
    result = {}
    
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)  # (7)!
            result.update({k: v[0] for k, v in qs.items()})  # (8)!
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"You may close this tab.")
    
        def log_message(self, *args):  # (9)!
            pass
    
    # 3. Open browser to /authorize
    auth_url = f"{INSTANCE}/oauth/authorize/?" + urllib.parse.urlencode({  # (10)!
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "read import",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print(f"Opening {auth_url}")
    webbrowser.open(auth_url)  # (11)!
    
    # 4. Wait for the redirect
    http.server.HTTPServer(("127.0.0.1", port), Handler).handle_request()  # (12)!
    
    if result.get("state") != state:  # (13)!
        raise SystemExit("State mismatch — possible CSRF.")
    if "error" in result:
        raise SystemExit(f"Authorization failed: {result['error']}")
    
    # 5. Exchange code for tokens
    resp = requests.post(f"{INSTANCE}/oauth/token/", data={  # (14)!
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": redirect_uri,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
    })
    resp.raise_for_status()  # (15)!
    tokens = resp.json()
    
    # 6. Use the access token
    me = requests.get(  # (16)!
        f"{INSTANCE}/api/v2/auth/me/",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    ).json()
    print(f"Logged in as {me['username']} (scopes: {me['scopes']})")
    ```
    
    1. `secrets.token_bytes(32)` draws 32 cryptographically-random bytes; `urlsafe_b64encode` renders them as text using the URL-safe alphabet (`-`/`_` instead of `+`/`/`); `.rstrip(b"=")` drops the trailing `=` padding that PKCE forbids; `.decode()` turns the resulting `bytes` into a `str`. This is your secret — you keep it and reveal it only in step 5.
    2. The challenge is the verifier's SHA-256 hash, encoded the same way. In execution order:
        1. `hashlib.sha256(verifier.encode()).digest()` hashes the verifier's bytes and returns the raw digest (*not* the hex form).
        2. `base64.urlsafe_b64encode(…)` base64url-encodes that digest.
        3. `.rstrip(b"=")` strips the padding.
        4. `.decode()` turns the `bytes` into a `str`.
    
        The server reruns this exact sequence on the verifier you send later and compares, so a hex digest or leftover padding would break the match. The challenge is the only part of the secret that travels in the URL.
    
    3. A second, independent random value. You send it in the URL and check it when the browser returns (in step 4); if it doesn't come back unchanged, the redirect wasn't yours. Unrelated to PKCE — both protections run together.
    4. Port `0` is a wildcard: the OS hands you any free port instead of one you hardcode and risk colliding with.
    5. Ask the socket which port the OS actually assigned.
    6. Close this socket immediately so the real HTTP server (started in step 4) can bind that port. We opened it only to discover a free one.
    7. Parse the `?code=…&state=…` query string of the incoming redirect into a dict.
    8. `parse_qs` gives each value as a *list* (a query key can repeat), so take the first of each and copy it into the outer `result` dict, where the main thread reads it after the server returns.
    9. Override the handler's default logging so it doesn't print an access-log line to your console; `pass` means do nothing.
    10. `urlencode` turns the dict into a query string and percent-escapes each value, so `"read import"` becomes `read+import`.
    11. Launch the user's default browser at that URL. They log in and approve on our site; on approval we redirect their browser to your loopback `redirect_uri`.
    12. Bind the real server to the port from step 2 and serve exactly one request — the redirect — then return. `handle_request()` blocks until that single request arrives.
    13. Confirm the returned `state` equals the one you generated. A mismatch means a forged or stale redirect, so refuse to continue.
    14. Trade the one-time `code` for tokens within 60 seconds. `data=` sends the fields as `application/x-www-form-urlencoded`, which the token endpoint requires.
    15. Raise if the server answered 4XX/5XX — e.g. `400 invalid_grant` once the 60-second window lapses — rather than parsing an error body as if it were tokens.
    16. Call `/auth/me/` with the access token in an `Authorization: Bearer` header to see who the token represents and which scopes it carries. Store the `refresh_token` from step 5 for [refreshing](#refresh-token-grant) later.

=== "R"

    ```r
    library(openssl)
    library(base64enc)
    library(httpuv)
    library(httr2)
    
    INSTANCE  <- "https://yesterdays.today"
    CLIENT_ID <- "your-public-client-id"
    
    b64url <- function(x) {
      enc <- base64enc::base64encode(x)
      gsub("=+$", "", chartr("+/", "-_", enc))  # (1)!
    }
    
    # 1. PKCE
    verifier  <- b64url(openssl::rand_bytes(32))  # (2)!
    challenge <- b64url(openssl::sha256(charToRaw(verifier), raw = TRUE))  # (3)!
    state     <- b64url(openssl::rand_bytes(16))  # (4)!
    
    # 2. Loopback callback
    result <- new.env()  # (5)!
    server <- startServer("127.0.0.1", 0, list(  # (6)!
      call = function(req) {
        qs <- parseQueryString(req$QUERY_STRING)  # (7)!
        result$code  <- qs$code
        result$state <- qs$state
        result$error <- qs$error
        list(status = 200L, body = "You may close this tab.")
      }
    ))
    port <- server$getPort()  # (8)!
    redirect_uri <- sprintf("http://127.0.0.1:%d/callback", port)
    
    # 3. Open browser to /authorize
    auth_url <- paste0(  # (9)!
      INSTANCE, "/oauth/authorize/?",
      paste(
        paste0("response_type=", "code"),
        paste0("client_id=",     URLencode(CLIENT_ID, reserved = TRUE)),
        paste0("redirect_uri=",  URLencode(redirect_uri, reserved = TRUE)),
        paste0("scope=",         URLencode("read import", reserved = TRUE)),
        paste0("state=",         state),
        paste0("code_challenge=", challenge),
        paste0("code_challenge_method=", "S256"),
        sep = "&"
      )
    )
    cat("Opening", auth_url, "\n")
    browseURL(auth_url)  # (10)!
    
    # 4. Wait for the redirect
    while (is.null(result$code) && is.null(result$error)) {  # (11)!
      service()
      Sys.sleep(0.1)
    }
    stopServer(server)
    
    if (!identical(result$state, state)) {  # (12)!
      stop("State mismatch — possible CSRF.")
    }
    if (!is.null(result$error)) {
      stop("Authorization failed: ", result$error)
    }
    
    # 5. Exchange code for tokens
    resp <- request(paste0(INSTANCE, "/oauth/token/")) |>  # (13)!
      req_body_form(
        grant_type    = "authorization_code",
        code          = result$code,
        redirect_uri  = redirect_uri,
        client_id     = CLIENT_ID,
        code_verifier = verifier
      ) |>
      req_perform()
    tokens <- resp_body_json(resp)
    
    # 6. Use the access token
    me <- request(paste0(INSTANCE, "/api/v2/auth/me/")) |>  # (14)!
      req_headers(Authorization = paste("Bearer", tokens$access_token)) |>
      req_perform() |>
      resp_body_json()
    
    cat(sprintf("Logged in as %s (scopes: %s)\n",
                me$username, paste(me$scopes, collapse = ", ")))
    ```
    
    1. base64url isn't built into base R, so this helper makes one: `chartr("+/", "-_", enc)` swaps standard base64's `+`/`/` for the URL-safe `-`/`_`, and `gsub("=+$", "", …)` strips trailing `=` padding. Used for the verifier, challenge, and state below.
    2. 32 cryptographically-random bytes, base64url-encoded. This is your secret — kept locally and revealed only in step 5.
    3. The challenge is the SHA-256 of the verifier. `charToRaw` turns the verifier string into the bytes `sha256` expects, and `raw = TRUE` returns the raw digest (not a hex string) so the encoding matches what the server recomputes. Hash first, then encode.
    4. A second, independent random value for the CSRF check in step 4. Separate from PKCE.
    5. An *environment*, not a list, because environments are mutable by reference: the server callback (which runs later, in its own scope) can write into it and this outer code will see the values.
    6. Bind a loopback server on port `0` so the OS picks a free port. The `call` function runs on each incoming request — here, the single redirect.
    7. Inside the callback, parse the redirect's query string and stash `code`/`state`/`error` into the `result` environment.
    8. Ask the running server which ephemeral port it bound.
    9. Build the authorization URL by hand. `URLencode(reserved = TRUE)` percent-escapes each value — e.g. the space in `"read import"` — so the query string is well-formed.
    10. Open the user's default browser at that URL. They log in and approve on our site; on approval we redirect their browser to `redirect_uri`.
    11. `httpuv` has no blocking "serve one request" call, so spin its event loop with `service()` (every 0.1 s) until the callback fills in `result$code` or `result$error`, then stop the server.
    12. `identical()` is an exact, type-safe comparison of the returned `state` against the one you sent; a mismatch means a forged or stale redirect.
    13. Trade the one-time `code` for tokens within 60 seconds. `httr2` builds the request as a pipeline — `req_body_form` sets the `application/x-www-form-urlencoded` fields and `req_perform` sends it.
    14. Call `/auth/me/` with the access token in a `Bearer` `Authorization` header to see the token's user and scopes. Keep the `refresh_token` from the previous step for [refreshing](#refresh-token-grant) later.
