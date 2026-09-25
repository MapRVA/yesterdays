# Apps

Third-party clients (desktop importers, web apps, scripts) can register themselves dynamically with any Yesterdays instance, then drive the standard OAuth2 authorization-code flow on behalf of users.

This avoids the need for an app developer to manually register a client on every instance their users might want to connect to.

## Register a new app

```
POST /api/v2/apps/
```

No authentication required. The server returns the `client_secret` in plaintext exactly once — store it securely; the server keeps only a hashed copy.

### Parameters

| Parameter       | Type   | Description |
|-----------------|--------|-------------|
| `name`          | string | Human-readable name shown to users on the consent screen. |
| `redirect_uris` | string | Space-separated list of allowed redirect URIs. For native apps using a loopback callback, register `http://127.0.0.1/callback` — the port is ignored at match time per RFC 8252. |
| `client_type`   | string | Either `confidential` or `public`. Use `public` for native or single-page apps that cannot keep a secret. |

### Example request

=== "curl"

    ```bash
    curl -X POST "https://yesterdays.today/api/v2/apps/" \
      -H "Content-Type: application/json" \
      -d '{
        "name": "My Yesterdays Tool",
        "redirect_uris": "http://127.0.0.1/callback",
        "client_type": "public"
      }'
    ```

=== "Python"

    ```python
    import requests

    response = requests.post(
        "https://yesterdays.today/api/v2/apps/",
        json={
            "name": "My Yesterdays Tool",
            "redirect_uris": "http://127.0.0.1/callback",
            "client_type": "public",
        },
    )
    app = response.json()
    ```

### Example response

```json
{
    "id": 12,
    "name": "My Yesterdays Tool",
    "client_id": "AB12CD34EF56...",
    "client_secret": "",
    "client_type": "public",
    "redirect_uris": "http://127.0.0.1/callback"
}
```

For a `confidential` app, `client_secret` will contain the freshly generated secret. For a `public` app it will be empty.

## Using the credentials

After registration, follow the normal OAuth2 authorization-code flow against `/oauth/authorize/` and `/oauth/token/`. PKCE is required for all clients.

Users will see a consent screen the first time your app requests authorization, showing the `name` you registered. Their approval is remembered: later authorization requests for already-granted scopes skip the screen entirely, until the user revokes your app from their account settings. Requesting a scope the user hasn't yet granted re-prompts them. See [Authentication](authentication.md#authorization-request) for details.

## Notes for app developers

- **Per-instance registration.** A `client_id` is only valid on the instance that issued it. Cache credentials per-instance on first contact.
- **Public clients must use PKCE.** The token endpoint will reject auth-code exchanges from public clients without a verifier.
- **Loopback redirects.** Desktop apps that bind to an ephemeral port should register `http://127.0.0.1/callback` (no port). The server matches loopback URIs without comparing ports.
