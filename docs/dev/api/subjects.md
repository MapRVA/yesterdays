# Subjects

Subjects are the things that appear in images — buildings, people, monuments, parks, and more.
Each subject is linked to a [Wikidata](https://www.wikidata.org/) item, which provides structured metadata like descriptions and identifiers.

Only subjects that appear in at least one public image are returned by the API.

## List all subjects

```
GET /api/v2/subjects/
```

Returns a paginated list of subjects with their Wikidata metadata and image counts.

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/subjects/"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/subjects/")
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/subjects/") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

### Example response

```json
{
    "count": 263,
    "next": "https://yesterdays.maprva.org/api/v2/subjects/?page=2",
    "previous": null,
    "results": [
        {
            "id": 22,
            "title": "Hollywood Cemetery",
            "slug": "hollywood-cemetery",
            "description": "cemetery in Richmond, Virginia, United States",
            "wikidata": {
                "wikidata_id": "Q5882648",
                "uri": "https://www.wikidata.org/entity/Q5882648",
                "title": "Hollywood Cemetery",
                "description": "cemetery in Richmond, Virginia, United States"
            },
            "image_count": 208,
            "images_url": "https://yesterdays.maprva.org/api/v2/images/?subject=22"
        }
    ]
}
```

## Get a single subject

```
GET /api/v2/subjects/{id}/
```

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/subjects/4/"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/subjects/4/")
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/subjects/4/") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

## Get a subject's geometry

```
GET /api/v2/subjects/{id}/geometry/
```

Returns the physical outline of a subject as a GeoJSON FeatureCollection.
These geometries come from [OpenStreetMap](https://www.openstreetmap.org/) (OSM)— for a building, this would be its footprint on the map.
Some subjects span multiple OSM elements (e.g., a street made up of several ways), so the FeatureCollection may contain more than one feature.

!!! info
    Not all subjects have geometry. Many subjects (such as people or vehicles) don't have a physical footprint. Or, they might not be tagged correctly in OSM. This endpoint returns an empty FeatureCollection in those cases.

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/subjects/4/geometry/"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/subjects/4/geometry/")
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/subjects/4/geometry/") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

### Example response

```json
{
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [[[
                    [-77.4332007, 37.5396989],
                    [-77.4326961, 37.5394044],
                    [-77.43245, 37.5396696],
                    [-77.4324062, 37.5397171],
                    [-77.4324715, 37.5397549],
                    [-77.432833, 37.5399661],
                    [-77.4329107, 37.540012],
                    [-77.43296, 37.5399595],
                    [-77.4332007, 37.5396989]
                ]]]
            },
            "properties": {
                "osm_id": "way/113023444"
            }
        }
    ]
}
```

## Fields

| Field         | Type    | Description |
|---------------|---------|-------------|
| `id`          | integer | Unique identifier |
| `title`       | string  | Name of the subject |
| `slug`        | string  | URL-friendly name |
| `description` | string  | Description of the subject, sourced from Wikidata (might fall back to placeholder) |
| `wikidata`    | object  | Wikidata metadata (see below) |
| `image_count` | integer | Number of public images tagged with this subject |
| `images_url`  | string  | API link to browse images of this subject |

### Wikidata object

| Field         | Type   | Description |
|---------------|--------|-------------|
| `wikidata_id` | string | Wikidata item ID (e.g., `"Q5882648"`) |
| `uri`         | string | Full Wikidata URI |
| `title`       | string | Title from Wikidata |
| `description` | string | Short description from Wikidata |

## Filtering

| Parameter | Description |
|-----------|-------------|
| `slug`    | Filter by one or more exact, comma-separated slugs, matching any listed subject (e.g., `?slug=hollywood-cemetery,main-street-station`) |

## Ordering

| Parameter     | Description |
|---------------|-------------|
| `title`       | Sort alphabetically by title (default) |
| `image_count` | Sort by number of images |

Use `ordering=-image_count` to see the most-photographed subjects first.
