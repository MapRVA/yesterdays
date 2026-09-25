# Licenses

The set of image licenses recognized by a given Yesterdays instance.

Different instances may publish different license lists. Import clients should fetch this endpoint once per session and use the `name` values to validate the `license_name` field on every image before [committing an import](images.md#import-a-new-image) — the server rejects unknown license names rather than auto-creating them.

## List all licenses

```
GET /api/v2/licenses/
```

Returns the full license set as an **unpaginated JSON array**, alphabetical by `name`. No authentication required.

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.today/api/v2/licenses/"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.today/api/v2/licenses/")
    licenses = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.today/api/v2/licenses/") |>
      req_perform()
    licenses <- resp_body_json(resp)
    ```

### Example response

```json
[
    {
        "name": "All Rights Reserved",
        "display_name": "All Rights Reserved",
        "permalink": "https://www.flickrhelp.com/hc/en-us/articles/10710266545556"
    },
    {
        "name": "CC BY 4.0",
        "display_name": "CC BY 4.0",
        "permalink": "https://creativecommons.org/licenses/by/4.0/"
    },
    {
        "name": "Public Domain Mark",
        "display_name": "Public Domain Mark",
        "permalink": "https://creativecommons.org/publicdomain/mark/1.0/"
    }
]
```

## Get a single license

```
GET /api/v2/licenses/{id}/
```

Same fields as the list entry. Most clients don't need this — fetch the list once and look up by `name`.

## Fields

| Field          | Type   | Description |
|----------------|--------|-------------|
| `name`         | string | Canonical identifier. Use this when matching against the `license_name` field of an import. |
| `display_name` | string | Human-readable name shown on the site. |
| `permalink`    | string | URL to the license's reference page. May be empty. |

## Matching is case-insensitive

The server compares `license_name` against this set with case-insensitive equality. `"cc by 4.0"` and `"CC BY 4.0"` both match the canonical row. Client-side, doing a case-insensitive comparison against the `name` values returned here mirrors server behavior exactly.
