# Georeferences

Georeferences are the map placements that our community has contributed.
There are two types:

- **Point georeferences**: the location of the "camera".
- **From-above georeferences**: a polygon outlining the area covered by an image taken from high up, such as from an airplane.

Both endpoints return [GeoJSON](https://geojson.org/) FeatureCollections.

!!! tip
    These endpoints only return the **most recent** georeference per image. If an image has been georeferenced multiple times, only the latest placement is included. To see all georeferences for a specific image, use the [image detail endpoint](images.md#get-a-single-image).

## Point georeferences

```
GET /api/v2/georeferences/
```

Returns the **most recent** point georeference per image as a GeoJSON FeatureCollection.
Each feature is a user-submitted point representing the location of the "camera".

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/georeferences/"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/georeferences/")
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/georeferences/") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

### Example response

```json
{
    "type": "FeatureCollection",
    "count": 8969,
    "next": "https://yesterdays.maprva.org/api/v2/georeferences/?page=2",
    "previous": null,
    "features": [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [-77.409651, 37.536789]
            },
            "properties": {
                "id": 5701,
                "image_id": 5934,
                "image_title": "N. 28th and Q Streets",
                "image_thumbnail": "https://cdn.maprva.org/7416452b52d0f9fdbe1c_thumb",
                "direction": 136.0,
                "confidence": "medium",
                "georeferenced_by": "mhpob",
                "georeferenced_at": "2025-12-16T12:55:01Z",
                "validation_count": 0
            }
        }
    ]
}
```

Pagination works the same as the rest of the API (`page` and `page_size` query parameters), but the `count`, `next`, and `previous` fields are included directly inside the GeoJSON FeatureCollection rather than wrapping it.

### Point georeference fields

| Field              | Type    | Description |
|--------------------|---------|-------------|
| `id`               | integer | Unique identifier for this georeference |
| `image_id`         | integer | ID of the georeferenced image |
| `image_title`      | string  | Title of the image |
| `image_thumbnail`  | string  | URL to the image thumbnail |
| `direction`        | number  | Compass direction the "camera" was facing (degrees, 0 = north) |
| `confidence`       | string  | `"low"`, `"medium"`, or `"high"`|
| `georeferenced_by` | string  | OpenStreetMap username of the contributor |
| `georeferenced_at` | string  | ISO 8601 timestamp |
| `validation_count` | integer | Number of community validations |

## From-above georeferences

```
GET /api/v2/from-above-georeferences/
```

Returns the **most recent** polygon georeference per image for bird's-eye and from-above images as a GeoJSON FeatureCollection.
Each feature's geometry is a polygon outlining the area covered by the image.

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/from-above-georeferences/"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/from-above-georeferences/")
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/from-above-georeferences/") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

### Example response

```json
{
    "type": "FeatureCollection",
    "count": 93,
    "next": null,
    "previous": null,
    "features": [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [-77.424245114, 37.539632546],
                    [-77.427029848, 37.53543474],
                    [-77.435582592, 37.537152634],
                    [-77.430173107, 37.544017523],
                    [-77.424245114, 37.539632546]
                ]]
            },
            "properties": {
                "id": 123,
                "image_id": 35157,
                "image_title": "Aerial View of Downtown Richmond",
                "image_thumbnail": "https://cdn.maprva.org/51cb74d460edec3c3d98_thumb",
                "confidence": "high",
                "georeferenced_by": "John Pole",
                "georeferenced_at": "2026-02-20T04:41:09Z",
                "validation_count": 0
            }
        }
    ]
}
```

### From-above georeference fields

| Field              | Type    | Description |
|--------------------|---------|-------------|
| `id`               | integer | Unique identifier |
| `image_id`         | integer | ID of the georeferenced image |
| `image_title`      | string  | Title of the image |
| `image_thumbnail`  | string  | URL to the image thumbnail |
| `confidence`       | string  | `"low"`, `"medium"`, or `"high"` |
| `georeferenced_by` | string  | OpenStreetMap username of the contributor |
| `georeferenced_at` | string  | ISO 8601 timestamp |
| `validation_count` | integer | Number of community validations |

!!! tip
    The `confidence_notes` field is available on georeference objects within the [image detail](images.md#get-a-single-image) response, but is omitted from these GeoJSON endpoints to keep responses lightweight.

## Bounding box filtering

Both georeference endpoints support spatial filtering with the `in_bbox` parameter.
This lets you fetch only the georeferences within a geographic area — perfect for map-based applications.
Point georeferences are included when their point falls inside the box. From-above georeferences are included when any part of their coverage polygon intersects the box, even if the polygon extends beyond it.

The format is `in_bbox=west,south,east,north` (minimum longitude, minimum latitude, maximum longitude, maximum latitude):

=== "curl"

    ```bash
    # Georeferences in downtown Richmond
    curl "https://yesterdays.maprva.org/api/v2/georeferences/?in_bbox=-77.45,37.53,-77.43,37.55"
    ```

=== "Python"

    ```python
    import requests

    # Georeferences in downtown Richmond
    response = requests.get("https://yesterdays.maprva.org/api/v2/georeferences/", params={
        "in_bbox": "-77.45,37.53,-77.43,37.55",
    })
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    # Georeferences in downtown Richmond
    resp <- request("https://yesterdays.maprva.org/api/v2/georeferences/") |>
      req_url_query(in_bbox = "-77.45,37.53,-77.43,37.55") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

## Other filters

Both endpoints support these filters:

| Parameter          | Type    | Description |
|--------------------|---------|-------------|
| `image`            | string  | Filter by one or more comma-separated image IDs, matching any listed image |
| `source`           | string  | Filter by one or more comma-separated source IDs, matching any listed source |
| `collection`       | string  | Filter by one or more comma-separated collection IDs, matching any listed collection |
| `subject`          | string  | Filter by one or more comma-separated subject database IDs or Wikidata Q-IDs, matching any listed subject (e.g., `22,Q5882648`) |
| `confidence`       | string  | Filter by confidence level: `low`, `medium`, or `high` |
| `georeferenced_by` | string  | Filter by one or more comma-separated OpenStreetMap user IDs, matching any listed contributor (e.g., `100,200`). You can use the [users endpoint](users.md) to look these up. |
| `year_min`         | number  | Include georeferences whose image's date range overlaps with or follows this year |
| `year_max`         | number  | Include georeferences whose image's date range overlaps with or precedes this year |

The point georeferences endpoint also supports:

| Parameter    | Type    | Description |
|--------------|---------|-------------|
| `from_above` | boolean | `true` for from-above images, `false` for standard images. Useful because from-above images can have both point and polygon georeferences, so `from_above=false` lets you exclude them from point results. |

### Example: combining filters

Find all high-confidence georeferences from the Library of Virginia:

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/georeferences/?source=1&confidence=high"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/georeferences/", params={
        "source": 1,
        "confidence": "high",
    })
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/georeferences/") |>
      req_url_query(source = 1, confidence = "high") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

## Ordering

| Parameter | Description |
|-----------|-------------|
| `georeferenced_at` | Sort by when the georeference was submitted |
| `confidence` | Sort by confidence level |
| `validation_count` | Sort by number of community validations |

Prefix with `-` to sort descending. Default ordering is `-georeferenced_at` (most recently georeferenced first).
