# Statistics

## Site statistics

```
GET /api/v2/stats/
```

Returns current site-wide statistics. This is a simple, non-paginated response.

All counts cover only public collections belonging to public sources.

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.today/api/v2/stats/"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.today/api/v2/stats/")
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.today/api/v2/stats/") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

### Example response

```json
{
    "name": "Yesterdays",
    "total_sources": 5,
    "total_collections": 49,
    "total_images": 35184,
    "georeferenced_images": 9014,
    "total_georeferences": 9210,
    "confidence_breakdown": {
        "low": 183,
        "medium": 1174,
        "high": 7657
    }
}
```

### Fields

| Field | Type | Description |
|---|---|---|
| `name` | string | Site title configured for this instance |
| `total_sources` | integer | Number of public sources |
| `total_collections` | integer | Number of public collections |
| `total_images` | integer | Images eligible for georeferencing (excludes duplicates and images marked as will-not-georeference) |
| `georeferenced_images` | integer | Number of distinct images that have been georeferenced |
| `total_georeferences` | integer | Total number of georeference submissions (includes superseded and improved georeferences) |
| `confidence_breakdown` | object | Georeferenced image counts by confidence level |
| `confidence_breakdown.low` | integer | Images whose most recent georeference is low confidence |
| `confidence_breakdown.medium` | integer | Images whose most recent georeference is medium confidence |
| `confidence_breakdown.high` | integer | Images whose most recent georeference is high confidence |
