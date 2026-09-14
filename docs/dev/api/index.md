# API

The Yesterdays API gives you free access to our entire catalog of historical images, georeferences, and subjects.
You can use it to build apps, bots, maps, research tools...whatever you like.

**No API key is required.** The API is read-only and publicly accessible.

## Base URL

All endpoints live under:

```
https://yesterdays.maprva.org/api/v2/
```

## Quick start

Here's how to fetch a single image:

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/images/1234/"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/images/1234/")
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/images/1234/") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

Or list all available sources:

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/sources/"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/sources/")
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/sources/") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

You can also just open these URLs in your browser — they work the same way.

## Endpoints

| Endpoint | Description |
|---|---|
| [`/api/v2/users/`](users.md) | Contributors who have georeferenced images |
| [`/api/v2/sources/`](sources.md) | Institutions that contributed images |
| [`/api/v2/collections/`](collections.md) | Groups of images within each source |
| [`/api/v2/images/`](images.md) | The images themselves |
| [`/api/v2/licenses/`](licenses.md) | Licenses recognized when importing images |
| [`/api/v2/subjects/`](subjects.md) | Buildings, people, and other tagged subjects |
| [`/api/v2/georeferences/`](georeferences.md) | Point locations as GeoJSON |
| [`/api/v2/from-above-georeferences/`](georeferences.md#from-above-georeferences) | Polygon coverage areas as GeoJSON |
| [`/api/v2/search/semantic/`](search.md#semantic-search) | Search images by meaning (CLIP) |
| [`/api/v2/search/text/`](search.md#text-search) | Search images by text matching |
| [`/api/v2/search/in-view/`](search.md#in-view-search) | Find images that have a coordinate in view |
| [`/api/v2/stats/`](statistics.md#site-statistics) | Site-wide statistics |
| [`/api/v2/activity/`](activity.md#activity-feed) | Recent activity feed |

## Response format

All responses are JSON. Georeference endpoints return [GeoJSON](https://geojson.org/) FeatureCollections.

## Pagination

List endpoints return paginated results with 50 items per page by default. You can adjust the page size with the `page_size` query parameter (max 100). The response includes `count`, `next`, and `previous` fields to help you page through results:

```json
{
    "count": 2340,
    "next": "https://yesterdays.maprva.org/api/v2/images/?page=2",
    "previous": null,
    "results": [...]
}
```

Use the `page` query parameter to request a specific page:

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/images/?page=3"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/images/", params={"page": 3})
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/images/") |>
      req_url_query(page = 3) |>
      req_perform()
    data <- resp_body_json(resp)
    ```

## Filtering and ordering

Most endpoints support filtering and ordering via query parameters. Each endpoint's documentation lists the available filters. For example, to find all images from a specific collection:

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/images/?collection=5"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/images/", params={"collection": 5})
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/images/") |>
      req_url_query(collection = 5) |>
      req_perform()
    data <- resp_body_json(resp)
    ```

To change the sort order, use the `ordering` parameter:

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/images/?ordering=original_date"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/images/", params={"ordering": "original_date"})
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/images/") |>
      req_url_query(ordering = "original_date") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

Prefix with `-` to sort descending:

=== "curl"

    ```bash
    curl "https://yesterdays.maprva.org/api/v2/images/?ordering=-original_date"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.maprva.org/api/v2/images/", params={"ordering": "-original_date"})
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.maprva.org/api/v2/images/") |>
      req_url_query(ordering = "-original_date") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

## Need help?

If you have questions about the API or want to share what you've built, join us in the `#yesterdays` channel on the [OpenStreetMap US Slack](https://openstreetmap.us/slack/).
You can also [contact us directly](/contact).
