# Search

The Yesterdays API offers three search endpoints:

- **Semantic search** interprets textual input even if the exact words don't appear in the image's title or description. For example, searching "church steeple at sunset" will find visually similar images.
- **Text search** searches across titles, descriptions, comments, and georeference notes for text that closely resembles your query.
- **In-view search** finds images that depict a coordinate, ordered by distance.

Semantic and text search use the same response format. In-view search reports a distance instead of a similarity score.

## Semantic search

```
GET /api/v2/search/semantic/?q={query}
```

Uses a [CLIP](https://en.wikipedia.org/wiki/Contrastive_Language-Image_Pre-training) model to encode your query into a vector and find images whose visual content is most similar.

!!! info
    Semantic search only works on images that have been processed by our CLIP model. Newly added images may not immediately appear in search results.

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/search/semantic/?q=church+on+a+hill"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/search/semantic/", params={
        "q": "church on a hill",
    })
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/search/semantic/") |>
      req_url_query(q = "church on a hill") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

### Example response

```json
{
    "count": 234,
    "page": 1,
    "page_size": 20,
    "query": "church on a hill",
    "results": [
        {
            "id": 657,
            "title": "St. Johns Church area 1",
            "permalink": "https://cdn.maprva.org/71086e03d2fefa626a0e",
            "thumbnail": "https://cdn.maprva.org/71086e03d2fefa626a0e_thumb",
            "original_date": "1965",
            "date_display": "1965",
            "similarity": 0.8341,
            "collection": {
                "id": 125,
                "name": "1965 Richmond Esthetic Survey and Historic Building Survey",
                "slug": "1965-esthetic-survey",
                "source_name": "Library of Virginia"
            },
            "detail_url": "https://yesterdays.maprva.org/api/v2/images/657/"
        }
    ]
}
```

## Text search

```
GET /api/v2/search/text/?q={query}
```

Uses PostgreSQL trigram word similarity to search across multiple text fields for each image:

- Image title
- Image description
- Community comments
- Georeference notes

Results are ranked by how closely the query matches, with the best matches first.

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/search/text/?q=broad+street"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/search/text/", params={
        "q": "broad street",
    })
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/search/text/") |>
      req_url_query(q = "broad street") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

### Example response

```json
{
    "count": 89,
    "page": 1,
    "page_size": 20,
    "query": "broad street",
    "results": [
        {
            "id": 1067,
            "title": "E. Broad Street",
            "permalink": "https://cdn.maprva.org/09035fe9fe8c4411ccfc",
            "thumbnail": "https://cdn.maprva.org/09035fe9fe8c4411ccfc_thumb",
            "original_date": "1941-1949",
            "date_display": "1941-1949",
            "similarity": 0.9412,
            "collection": {
                "id": 37,
                "name": "Mary Wingfield Scott Photograph Collection",
                "slug": "mary-wingfield-scott-photograph-collection",
                "source_name": "The Valentine"
            },
            "detail_url": "https://yesterdays.maprva.org/api/v2/images/1067/"
        }
    ]
}
```

### Text search parameters

| Parameter   | Type   | Default | Description |
|-------------|--------|---------|-------------|
| `threshold` | number | 0.7     | Distance threshold (0 to 1). Lower values return fewer, more precise matches; higher values return more results with looser matching. |

## In-view search

```
GET /api/v2/search/in-view/?lat={latitude}&lon={longitude}
```

Returns images that depict the specified coordinate, ordered by proximity.

### Directional filtering

For georeferences with a recorded camera direction, the coordinate must fall within the camera's viewing cone. The default cone is **40°** wide, or 20° to either side of the recorded direction. Its width is controlled by a server setting and may change in the future.

Georeferences without a recorded direction are included based on distance alone.

Examples:

| | distance | camera direction | bearing to your point | returned |
|---|---|---|---|---|
| Facing the point | 35 m | 53° | 55.4° | Yes (2.4° difference) |
| Facing away | 50 m | 340° | 73.3° | No (93.3° difference) |
| Facing the point from a distance | 470 m | 180° | 183.7° | Yes (3.7° difference) |

!!! info
    This endpoint considers point georeferences only. For aerial photographs with polygon coverage, use [`/api/v2/from-above-georeferences/`](georeferences.md#from-above-georeferences).

`radius` is optional. Without it, the endpoint reads results in distance order until it has filled the requested page.

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/search/in-view/?lat=37.5407&lon=-77.4360&radius=500"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/search/in-view/", params={
        "lat": 37.5407,
        "lon": -77.4360,
        "radius": 500,
    })
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/search/in-view/") |>
      req_url_query(lat = 37.5407, lon = -77.4360, radius = 500) |>
      req_perform()
    data <- resp_body_json(resp)
    ```

### Example response

```json
{
    "latitude": 37.5407,
    "longitude": -77.436,
    "radius": 500.0,
    "page": 1,
    "page_size": 20,
    "count": 63,
    "has_more": true,
    "results": [
        {
            "id": 10470,
            "title": "Capitol Square",
            "permalink": "https://cdn.maprva.org/71086e03d2fefa626a0e",
            "thumbnail": "https://cdn.maprva.org/71086e03d2fefa626a0e_thumb",
            "original_date": "1908",
            "date_display": "1908",
            "distance_m": 42.7,
            "georeference": {
                "latitude": 37.540422,
                "longitude": -77.435884,
                "direction": 270,
                "confidence": "high"
            },
            "collection": {
                "id": 37,
                "name": "Mary Wingfield Scott Photograph Collection",
                "slug": "mary-wingfield-scott-photograph-collection",
                "source_name": "The Valentine"
            },
            "detail_url": "https://yesterdays.maprva.org/api/v2/images/10470/"
        }
    ]
}
```

### In-view search parameters

| Parameter | Type   | Description |
|-----------|--------|-------------|
| `lat`     | number | Latitude of the point to search around (required, -90 to 90) |
| `lon`     | number | Longitude of the point to search around (required, -180 to 180) |
| `radius`  | number | Optional bound in **metres**. Values above 50,000 are clamped to 50,000. |

In-view search also accepts the `page`, `page_size`, and filter parameters described below. It does not use `q`.

### In-view search response

The envelope differs from the other two endpoints:

| Field | Type | Description |
|---|---|---|
| `latitude`, `longitude` | number | The point that was searched around |
| `radius` | number or null | The radius actually applied, after clamping; `null` if none was given |
| `count` | integer or null | Total matching images after all filters. `null` unless you supply a `radius`. |
| `has_more` | boolean | Whether another page of results follows |

!!! warning "`count` is null without a radius"
    An unbounded search can include every otherwise eligible point-georeferenced image. Supply a `radius` to receive a total; otherwise, use `has_more` to continue paging.

Each result replaces `similarity` with:

| Field | Type | Description |
|---|---|---|
| `distance_m` | number | Distance in metres from your point to the image's georeference |
| `georeference` | object | The image's most recent georeference (`latitude`, `longitude`, `direction`, `confidence`). `direction` is the compass bearing the camera faced, or `null` if unrecorded. |

The endpoint returns at most 1,000 results: `page` × `page_size` must not exceed 1,000.

## Shared parameters

The following parameters are available across the search endpoints, subject to the notes below:

### Query

| Parameter | Type   | Description |
|-----------|--------|-------------|
| `q`       | string | Search query (required for semantic and text search, max 500 characters; not used by in-view search) |

### Pagination

| Parameter   | Type    | Default | Description |
|-------------|---------|---------|-------------|
| `page`      | integer | 1       | Page number |
| `page_size` | integer | 20      | Results per page (max 100) |

### Filters

| Parameter       | Type    | Description |
|-----------------|---------|-------------|
| `georeferenced` | boolean | `true` for georeferenced images only, `false` for un-georeferenced |
| `year_min`      | integer | Include images whose date range overlaps with or follows this year |
| `year_max`      | integer | Include images whose date range overlaps with or precedes this year |
| `source`        | string  | Filter by one or more comma-separated source IDs, matching any listed source |
| `collection`    | string  | Filter by one or more comma-separated collection IDs, matching any listed collection |
| `subject`       | string  | Filter by one or more comma-separated subject database IDs or Wikidata Q-IDs, matching any listed subject (e.g., `22,Q5882648`) |

### Example: combining search with filters

Find images matching "hotel" from the Library of Virginia, taken before 1920:

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/search/text/?q=hotel&source=1&year_max=1920"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/search/text/", params={
        "q": "hotel",
        "source": 1,
        "year_max": 1920,
    })
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/search/text/") |>
      req_url_query(q = "hotel", source = 1, year_max = 1920) |>
      req_perform()
    data <- resp_body_json(resp)
    ```

## Response format

Search endpoints use a different pagination format than other list endpoints, returning `page` and `page_size` instead of `next` and `previous` links.

Semantic and text search return a response with the following structure (see [In-view search](#in-view-search) for that endpoint's envelope):

| Field | Type | Description |
|---|---|---|
| `count` | integer | Total number of matching results |
| `page` | integer | Current page number |
| `page_size` | integer | Number of results per page |
| `query` | string | The search query that was submitted |
| `results` | array | List of matching images (see fields below) |

### Result fields

| Field | Type | Description |
|---|---|---|
| `id` | integer | Image ID |
| `title` | string | Image title |
| `permalink` | string | Direct URL to the image file (the transformed version if a rotation or mirror has been applied) |
| `thumbnail` | string | URL to a thumbnail |
| `original_date` | string | Date as recorded by the source |
| `date_display` | string | Human-friendly date display |
| `similarity` | number | Similarity score from 0 to 1 (higher is more similar). Scores are not directly comparable between semantic and text search. |
| `collection` | object | Collection info (id, name, slug, source_name) |
| `detail_url` | string | API link to the full image detail |
