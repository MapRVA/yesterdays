# Users

Users are the community members who have contributed georeferences to Yesterdays.
Everyone logs in with an [OpenStreetMap](https://www.openstreetmap.org/) account, and their OSM username is what you'll see here.

Only users who have georeferenced at least one public image are returned by the API.

## List all users

```
GET /api/v2/users/
```

Returns a paginated list of contributors with their georeference counts.

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.today/api/v2/users/"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.today/api/v2/users/")
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.today/api/v2/users/") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

### Example response

```json
{
    "count": 27,
    "next": null,
    "previous": null,
    "results": [
        {
            "id": 21907338,
            "username": "John Pole",
            "point_georeferences": 3576,
            "from_above_georeferences": 31
        },
        {
            "id": 846103,
            "username": "mhpob",
            "point_georeferences": 2390,
            "from_above_georeferences": 29
        }
    ]
}
```

## Get a single user

```
GET /api/v2/users/{id}/
```

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.today/api/v2/users/21907338/"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.today/api/v2/users/21907338/")
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.today/api/v2/users/21907338/") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

## Fields

| Field | Type | Description |
|---|---|---|
| `id` | integer | OpenStreetMap user ID (use this with the `georeferenced_by` filter on [georeference endpoints](georeferences.md)) |
| `username` | string | OpenStreetMap username |
| `point_georeferences` | integer | Number of point georeferences contributed |
| `from_above_georeferences` | integer | Number of from-above (polygon) georeferences contributed |

## Ordering

| Parameter | Description |
|---|---|
| `username` | Sort alphabetically by username (default) |
| `point_georeferences` | Sort by number of point georeferences |
| `from_above_georeferences` | Sort by number of from-above georeferences |

Use `ordering=-point_georeferences` to see the most active contributors first.
