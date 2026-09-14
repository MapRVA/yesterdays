# Collections

Collections group related images within a [source](sources.md).
For example, [*Cook Photograph Collection*](https://yesterdays.maprva.org/browse/the-valentine/cook-photograph-collection/) is one of several collections from The Valentine.

There are no "subcollections" in Yesterdays.
Just sources and collections.

## List all collections

```
GET /api/v2/collections/
```

Returns a paginated list of all public collections.

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/collections/"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/collections/")
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/collections/") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

### Example response

```json
{
    "count": 49,
    "next": null,
    "previous": null,
    "results": [
        {
            "id": 118,
            "name": "Cook Photograph Collection",
            "slug": "cook-photograph-collection",
            "url": "https://valentine.rediscoverysoftware.com/MADetailG.aspx?rID=PHC0047&db=group&dir=VALARCH",
            "description": "This collection of more than 10,0000 glass-plate and film negatives...",
            "source": {
                "id": 2,
                "name": "The Valentine",
                "slug": "the-valentine"
            },
            "image_count": 1957,
            "images_url": "https://yesterdays.maprva.org/api/v2/images/?collection=118"
        }
    ]
}
```

The full response would include all 49 collections in the `results` list. This example shows just one for brevity.

## Get a single collection

```
GET /api/v2/collections/{id}/
```

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/collections/118/"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/collections/118/")
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/collections/118/") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

## Create a collection

```
POST /api/v2/collections/
```

Creates a new collection inside an existing [source](sources.md).
Import tools need to call this if they want to upload to a new collection.

**Requires** an OAuth2 bearer token with the `import` scope, on a user with contributor (staff) status.
See [Authentication](authentication.md).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | yes | Name of the collection (max 200 characters). |
| `slug` | string | yes | URL-friendly identifier, unique within the source. |
| `source` | integer | yes | ID of the source this collection belongs to. |
| `url` | string | no | Link to the collection in the original archive. |
| `description` | string | no | Description of the collection. |
| `public` | boolean | no | If `true`, images committed into this collection are immediately visible to anonymous users. Defaults to `false` so newly-created collections stay hidden until you've populated them and reviewed metadata. |

### Example request

=== "curl"

    ```bash
    curl -X POST "https://yesterdays.maprva.org/api/v2/collections/" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d '{
        "name": "Edith K. Shelton Photograph Collection",
        "slug": "edith-k-shelton-photograph-collection",
        "source": 2,
        "url": "https://valentine.example/shelton",
        "description": "35mm slides taken by Edith K. Shelton..."
      }'
    ```

=== "Python"

    ```python
    import requests

    resp = requests.post(
        "https://yesterdays.maprva.org/api/v2/collections/",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "name": "Edith K. Shelton Photograph Collection",
            "slug": "edith-k-shelton-photograph-collection",
            "source": 2,
            "url": "https://valentine.example/shelton",
            "description": "35mm slides taken by Edith K. Shelton...",
        },
    )
    resp.raise_for_status()
    collection = resp.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/collections/") |>
      req_auth_bearer_token(TOKEN) |>
      req_body_json(list(
        name        = "Edith K. Shelton Photograph Collection",
        slug        = "edith-k-shelton-photograph-collection",
        source      = 2,
        url         = "https://valentine.example/shelton",
        description = "35mm slides taken by Edith K. Shelton..."
      )) |>
      req_perform()
    collection <- resp_body_json(resp)
    ```

### Example response (201 Created)

```json
{
    "id": 118,
    "name": "Edith K. Shelton Photograph Collection",
    "slug": "edith-k-shelton-photograph-collection",
    "source": 2,
    "url": "https://valentine.example/shelton",
    "description": "35mm slides taken by Edith K. Shelton...",
    "public": false
}
```

## Fields

| Field | Type | Description |
|---|---|---|
| `id` | integer | Unique identifier |
| `name` | string | Name of the collection |
| `slug` | string | URL-friendly name |
| `url` | string | Link to the collection in the original archive |
| `description` | string | Description of the collection |
| `source` | object | The [source](sources.md) this collection belongs to (id, name, slug) |
| `public` | boolean | Whether this collection is visible to non-admin users |
| `image_count` | integer | Number of images in this collection |
| `images_url` | string | API link to browse this collection's images |

## Filtering

| Parameter | Description |
|---|---|
| `source` | Filter by one or more comma-separated source IDs, matching any listed source (e.g., `?source=1,2`) |
| `slug` | Filter by one or more exact, comma-separated slugs, matching any listed collection |

## Ordering

| Parameter | Description |
|---|---|
| `name` | Sort alphabetically by collection name |
| `source__name` | Sort by source name, then collection name (default) |
