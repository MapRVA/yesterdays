# Images

Images are the heart of Yesterdays.
Most are old photographs, but they also include drawings, postcards, architectural sketches, and more.

The images endpoint has two different response formats: a list response for browsing, and a richer detail response with full metadata, georeferences, subjects, and comments.

## List images

```
GET /api/v2/images/
```

Returns a paginated list of images with basic metadata.
This response is intentionally lightweight and doesn't include georeferences, subjects, or comments.
Use the `detail_url` field to fetch the full details for any image.

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.today/api/v2/images/"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.today/api/v2/images/")
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.today/api/v2/images/") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

### Example response

```json
{
    "count": 37244,
    "next": "https://yesterdays.today/api/v2/images/?page=2",
    "previous": null,
    "results": [
        {
            "id": 5934,
            "title": "N. 28th and Q Streets",
            "thumbnail": "https://cdn.maprva.org/7416452b52d0f9fdbe1c_thumb",
            "creator": "Shelton, Edith Keesee--1898-1989",
            "original_date": "9/1956",
            "date_display": "9/1956",
            "from_above": false,
            "duplicate_of": null,
            "collection": {
                "id": 39,
                "name": "Edith K. Shelton Photograph Collection",
                "slug": "edith-k-shelton-photograph-collection",
                "source_name": "The Valentine"
            },
            "georeference_status": "georeferenced",
            "detail_url": "https://yesterdays.today/api/v2/images/5934/"
        }
    ]
}
```

### List fields

| Field | Type | Description |
|---|---|---|
| `id` | integer | Unique identifier |
| `title` | string | Title of the image |
| `thumbnail` | string | URL to a thumbnail of the image |
| `creator` | string | Creator/photographer name |
| `original_date` | string | Date string as recorded by the source |
| `date_display` | string | Human-friendly date display |
| `from_above` | boolean | Whether this is a from-above (bird's-eye) image |
| `duplicate_of` | integer or null | ID of the image this is a duplicate of, or `null` if not a duplicate |
| `collection` | object | Collection info (id, name, slug, source_name) |
| `georeference_status` | string | One of `"available"`, `"georeferenced"`, `"duplicate"`, or `"will_not_georef"` |
| `detail_url` | string | API link to the full detail for this image |


## Filtering

| Parameter | Type | Description |
|---|---|---|
| `source` | string | Filter by one or more comma-separated source IDs, matching any listed source |
| `collection` | string | Filter by one or more comma-separated collection IDs, matching any listed collection |
| `subject` | string | Filter by one or more comma-separated subject database IDs or Wikidata Q-IDs, matching any listed subject (e.g., `22,Q5882648`) |
| `creator` | string | Search creator name (case-insensitive, partial match) |
| `year_min` | number | Include images whose date range overlaps with or follows this year |
| `year_max` | number | Include images whose date range overlaps with or precedes this year |
| `georeferenced` | boolean | `true` for georeferenced images only, `false` for un-georeferenced |
| `from_above` | boolean | `true` for from-above images, `false` for standard images |

### Example: combining filters

Find all georeferenced images from the Valentine, taken between 1900 and 1920:

=== "curl"

    ```bash
    curl "https://yesterdays.today/api/v2/images/?source=2&georeferenced=true&year_min=1900&year_max=1920"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.today/api/v2/images/", params={
        "source": 2,
        "georeferenced": "true",
        "year_min": 1900,
        "year_max": 1920,
    })
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.today/api/v2/images/") |>
      req_url_query(source = 2, georeferenced = "true", year_min = 1900, year_max = 1920) |>
      req_perform()
    data <- resp_body_json(resp)
    ```

## Ordering

| Parameter | Description |
|---|---|
| `order` | Sort by collection order |
| `title` | Sort alphabetically by title |
| `original_date` | Sort by date |
| `created_at` | Sort by when the image was added to Yesterdays |
| `last_georeferenced_at` | Sort by when the image was most recently georeferenced |

Prefix with `-` to sort descending. For example, `ordering=original_date` gives oldest first, while `ordering=-original_date` gives newest first.

=== "curl"

    ```bash
    # Most recently georeferenced images first
    curl "https://yesterdays.today/api/v2/images/?ordering=-last_georeferenced_at"
    ```

=== "Python"

    ```python
    import requests

    # Most recently georeferenced images first
    response = requests.get("https://yesterdays.today/api/v2/images/", params={
        "ordering": "-last_georeferenced_at",
    })
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    # Most recently georeferenced images first
    resp <- request("https://yesterdays.today/api/v2/images/") |>
      req_url_query(ordering = "-last_georeferenced_at") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

Default ordering is `-order` (newest additions first).


## Get a single image

```
GET /api/v2/images/{id}/
```

Returns complete details for a single image, including its georeferences, subjects, comments, and license information.

### Example request

=== "curl"

    ```bash
    curl "https://yesterdays.today/api/v2/images/5934/"
    ```

=== "Python"

    ```python
    import requests

    response = requests.get("https://yesterdays.today/api/v2/images/5934/")
    data = response.json()
    ```

=== "R"

    ```r
    library(httr2)

    resp <- request("https://yesterdays.today/api/v2/images/5934/") |>
      req_perform()
    data <- resp_body_json(resp)
    ```

### Example response

```json
{
    "id": 5934,
    "title": "N. 28th and Q Streets",
    "permalink": "https://cdn.maprva.org/7416452b52d0f9fdbe1c",
    "thumbnail": "https://cdn.maprva.org/7416452b52d0f9fdbe1c_thumb",
    "original_url": "https://valentine.rediscoverysoftware.com/MADetailB.aspx?rID=PHC0039/-#V.91.42.2952&db=biblio&dir=VALARCH",
    "description": "35mm color slide of the intersection of N. 28th Street and Q Street in north Church Hill; image shows a horse with small wagon loaded with a bale of hay, stopped at a Yield sign; three children stand on sidewalk near horse; two-story house with siding in background; Richmond, Virginia.",
    "creator": "Shelton, Edith Keesee--1898-1989",
    "license": null,
    "original_date": "9/1956",
    "edtf_date": "1956-09",
    "date_display": "9/1956",
    "collection": {
        "id": 39,
        "name": "Edith K. Shelton Photograph Collection",
        "slug": "edith-k-shelton-photograph-collection",
        "source_name": "The Valentine"
    },
    "subjects": [
        {
            "id": 150,
            "title": "horse",
            "slug": "horse",
            "wikidata": {
                "wikidata_id": "Q726",
                "uri": "https://www.wikidata.org/entity/Q726",
                "title": "horse",
                "description": "domesticated four-footed mammal from the equine family"
            }
        }
    ],
    "from_above": false,
    "duplicate_of": null,
    "scale": null,
    "mirror": "none",
    "rotation": 0,
    "georeference_status": "georeferenced",
    "georeferences": [
        {
            "id": 5701,
            "latitude": 37.536789,
            "longitude": -77.409651,
            "direction": 136.0,
            "confidence": "medium",
            "confidence_notes": "",
            "georeferenced_by": "mhpob",
            "georeferenced_at": "2025-12-16T12:55:01Z",
            "validations": []
        }
    ],
    "from_above_georeferences": [],
    "comments": [
        {
            "id": 57,
            "text": "Other angle of #5933",
            "commented_by": "mhpob",
            "created_at": "2025-12-16T12:55:42Z"
        }
    ],
    "detail_url": "https://yesterdays.today/api/v2/images/5934/",
    "iiif_url": "https://cdn.maprva.org/images/5934/iiif/"
}
```

### Detail fields

The detail response includes all the [list fields](#list-fields), plus:

| Field                      | Type           | Description |
|----------------------------|----------------|-------------|
| `permalink`                | string         | Direct URL to the image file. If a rotation or mirror transform has been applied, this automatically points to the corrected version. |
| `original_url`             | string         | Link to the image in the original archive |
| `description`              | string         | Full description of the image |
| `license`                  | object or null | License info (display_name, permalink), or `null` if no license is available |
| `edtf_date`                | string         | Date in [EDTF](https://www.loc.gov/standards/datetime/) format, when available |
| `subjects`                 | array          | [Subjects](subjects.md) tagged in this image, with Wikidata metadata |
| `scale`                    | string         | Scale notation for maps and from-above imagery |
| `mirror`                   | string         | Mirror transform applied: `"none"`, `"h"` (horizontal), or `"v"` (vertical) |
| `rotation`                 | integer        | Clockwise rotation applied to this image: `0`, `90`, `180`, or `270` (degrees) |
| `georeferences`            | array          | Point georeferences (see below) |
| `from_above_georeferences` | array          | From-above polygon georeferences (see below) |
| `comments`                 | array          | Community comments on this image |
| `iiif_url`                 | string or null | Base URL for this image's IIIF Image Service (append `info.json` for metadata), or `null` if tiles haven't been generated yet |

### Georeference objects

Each georeference in the `georeferences` array represents someone's placement of this image on the map:

| Field              | Type    | Description |
|--------------------|---------|-------------|
| `id`               | integer | Unique identifier |
| `latitude`         | number  | Latitude of the photographer's location |
| `longitude`        | number  | Longitude of the photographer's location |
| `direction`        | number  | Compass direction the camera was facing (degrees, 0 = north) |
| `confidence`       | string  | `"low"`, `"medium"`, or `"high"` |
| `confidence_notes` | string  | Explanation for the chosen confidence level |
| `georeferenced_by` | string  | OpenStreetMap username of the contributor |
| `georeferenced_at` | string  | ISO 8601 timestamp |
| `validations`      | array   | Community validations (see below) |

### From-above georeference objects

The `from_above_georeferences` array contains polygon-based georeferences for bird's-eye and from-above images:

| Field              | Type    | Description |
|--------------------|---------|-------------|
| `id`               | integer | Unique identifier |
| `polygon`          | object  | GeoJSON Polygon geometry representing the area covered |
| `confidence`       | string  | `"low"`, `"medium"`, or `"high"` |
| `confidence_notes` | string  | Explanation for the chosen confidence level |
| `georeferenced_by` | string  | OpenStreetMap username of the contributor |
| `georeferenced_at` | string  | ISO 8601 timestamp |
| `validations`      | array   | Community validations |

### Validation objects

Validations appear inside georeference objects. They represent community review of a georeference:

| Field          | Type   | Description |
|----------------|--------|-------------|
| `validation`   | string | One of `"correct"`, `"uncertain"`, or `"incorrect"` |
| `validated_by` | string | OpenStreetMap username of the reviewer |
| `notes`        | string | Optional notes from the reviewer |
| `validated_at` | string | ISO 8601 timestamp |

### Comment objects

| Field          | Type    | Description |
|----------------|---------|-------------|
| `id`           | integer | Unique identifier |
| `text`         | string  | The comment text |
| `commented_by` | string  | OpenStreetMap username of the commenter |
| `created_at`   | string  | ISO 8601 timestamp |


## Import a new image

```
POST /api/v2/import/commit/
```

Adds a new image to a collection. Use this when you have an image that isn't yet in Yesterdays — for higher-resolution scans of *existing* catalog entries, use [Replace an image file](#replace-an-image-file) instead.

**Requires** an OAuth2 bearer token with the `import` scope, on a user with contributor (staff) status. See [Authentication](authentication.md).

Importing is a three-step flow:

1. Request an **upload slot** from Yesterdays (returns a presigned URL into our object storage).
2. PUT the file bytes directly to that URL.
3. POST to the commit endpoint with the slot's ID and the new image's metadata.

### Step 1: Request an upload slot

```
GET /api/v2/import/upload-url/?collection={id}&content_type={mime}
```

Creates a short-lived import slot and returns a presigned PUT URL into object storage. The slot expires after **15 minutes**; the URL must be used within that window. If you decide not to commit, release the slot with [Cancel a pending import](#cancel-a-pending-import).

| Parameter      | Type    | Required | Description |
|----------------|---------|----------|-------------|
| `collection`   | integer | yes      | Collection ID to import into. |
| `content_type` | string  | no       | MIME type of the file you will upload. Defaults to `image/jpeg`. Must match exactly when you upload — the presigned URL is signed with this value. |

Example response:

```json
{
    "slot_id": "b5e7c9f2-4a1b-49d3-8c7e-1234567890ab",
    "upload_url": "https://r2.example.com/.../imports/b5e7c9f2...?X-Amz-Signature=...",
    "upload_headers": { "Content-Type": "image/tiff" },
    "cdn_url": "https://cdn.maprva.org/imports/b5e7c9f2..."
}
```

### Step 2: Upload the file

PUT the file bytes to `upload_url` using the exact headers returned in `upload_headers`. Include a `Content-Length` header matching the file size.

The upload target is object storage directly — not Yesterdays. Do not send your Yesterdays access token on this request.

### Step 3: Commit the import

```
POST /api/v2/import/commit/
```

Creates the `Image` row and moves the uploaded file to its permanent location.

| Parameter       | Type    | Required | Description |
|-----------------|---------|----------|-------------|
| `slot_id`       | UUID    | yes      | The `slot_id` returned in Step 1. |
| `title`         | string  | yes      | Image title (max 500 characters). |
| `original_date` | string  | yes      | The date string exactly as recorded by the source archive (max 50 characters). Preserved alongside `edtf_date` so the original wording isn't lost — e.g. `"ca. 1900-1910"`, `"9/1956"`. |
| `edtf_date`     | string  | yes      | The same date parsed into [EDTF](https://www.loc.gov/standards/datetime/) format (max 50 characters). |
| `source_url`    | string  | no       | URL of the image in the original archive. |
| `description`   | string  | no       | Full description. |
| `creator`       | string  | no       | Creator or photographer name (max 100 characters). |
| `reference_id`  | string  | no       | The source archive's reference ID for this image (max 100 characters). |
| `license_name`  | string  | no       | Exact `name` of a [recognized license](licenses.md). Unknown values are rejected. |
| `rotation`      | integer | no       | Display rotation in degrees clockwise: `0`, `90`, `180`, or `270`. Default `0`. |
| `mirror`        | string  | no       | Display mirror: `"none"`, `"h"`, or `"v"`. Default `"none"`. |
| `subjects`      | array   | no       | List of [Subject](subjects.md) slugs to tag on the image, in the order given. Each must match an existing subject's `slug`; unknown slugs are rejected. Default `[]`. |

The image bytes are uploaded as-is — `rotation` and `mirror` are stored as display metadata and applied at render time, not baked into the file. If you want the canonical bytes to be physically oriented, bake the transform in *before* uploading.

Example response (201 Created):

```json
{ "image_id": 5934 }
```

### What happens after a successful commit

- A new `Image` row is created with the metadata you provided.
- The uploaded file is moved from the import slot's temporary location to `images/{image_id}/original.{ext}`, where `{ext}` is derived from the slot's `content_type`.
- `permalink` points at the moved file.
- The import slot is consumed and cannot be committed again.
- Any `subjects` you provided are tagged on the image, in the order given, and recorded in its activity history.
- A background task generates the thumbnail, display variant, and IIIF tiles from the source bytes.

### Errors

| HTTP | Error                                                       | Meaning                                                                  |
|------|-------------------------------------------------------------|--------------------------------------------------------------------------|
| 400  | `license_name: License '...' is not recognized.`            | The provided license isn't in this instance's set — fetch [Licenses](licenses.md) for the allowed values. |
| 400  | `edtf_date: Invalid EDTF date: ...`                         | The provided date doesn't parse as EDTF. |
| 400  | `title: This field is required.`                            | A required field is missing or empty. |
| 400  | `subjects: Object with slug=... does not exist.`            | One of the provided subject slugs doesn't match an existing subject. |
| 401  | `invalid_token`                                             | Access token missing, expired, or revoked. |
| 403  | `insufficient_scope` / permission denied                    | Token lacks the `import` scope, or the user is not a contributor. |
| 404  | `Import slot not found or does not belong to you.`          | Slot expired, doesn't exist, or was created by a different user. |
| 409  | `Image has not been uploaded to S3 yet.`                    | No object at the slot's storage key — Step 2 hasn't completed. |

### Cancel a pending import

```
POST /api/v2/import/cancel/
```

Releases one or more pending import slots and deletes their temporary uploaded files. Use this when you've requested a slot but decided not to commit, so the temporary upload isn't left orphaned (the server reaps abandoned slots eventually, but cancelling is courteous and immediate).

| Parameter  | Type  | Required | Description |
|------------|-------|----------|-------------|
| `slot_ids` | array | yes      | 1–100 slot UUIDs to cancel. |

Slot IDs that don't exist, belong to a different user, or have already been committed are silently ignored. The response counts only the slots actually deleted.

Example response:

```json
{ "deleted": 3 }
```

### Complete example

=== "curl"

    ```bash
    INSTANCE="https://yesterdays.today"
    TOKEN="your-access-token"
    COLLECTION_ID=39
    FILE=/path/to/photo.tif
    MIME=image/tiff

    # 1. Request an upload slot.
    SLOT=$(curl -s \
      -H "Authorization: Bearer $TOKEN" \
      "$INSTANCE/api/v2/import/upload-url/?collection=$COLLECTION_ID&content_type=$MIME")
    SLOT_ID=$(echo "$SLOT" | jq -r .slot_id)
    UPLOAD_URL=$(echo "$SLOT" | jq -r .upload_url)

    # 2. PUT the file bytes directly to object storage.
    curl -X PUT "$UPLOAD_URL" \
      -H "Content-Type: $MIME" \
      --data-binary "@$FILE"

    # 3. Commit the import.
    curl -X POST "$INSTANCE/api/v2/import/commit/" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d @- <<EOF
    {
      "slot_id": "$SLOT_ID",
      "title": "N. 28th and Q Streets",
      "original_date": "9/1956",
      "edtf_date": "1956-09",
      "creator": "Shelton, Edith Keesee",
      "license_name": "CC BY 4.0",
      "source_url": "https://valentine.example/...",
      "description": "35mm color slide...",
      "rotation": 0,
      "mirror": "none"
    }
    EOF
    ```

=== "Python"

    ```python
    import requests

    INSTANCE = "https://yesterdays.today"
    TOKEN = "your-access-token"
    COLLECTION_ID = 39
    FILE = "/path/to/photo.tif"
    MIME = "image/tiff"

    auth = {"Authorization": f"Bearer {TOKEN}"}

    # 1. Request an upload slot.
    slot = requests.get(
        f"{INSTANCE}/api/v2/import/upload-url/",
        params={"collection": COLLECTION_ID, "content_type": MIME},
        headers=auth,
    ).json()

    # 2. Stream the file directly to object storage.
    with open(FILE, "rb") as f:
        put = requests.put(
            slot["upload_url"],
            data=f,
            headers=slot["upload_headers"],
        )
        put.raise_for_status()

    # 3. Commit the import.
    resp = requests.post(
        f"{INSTANCE}/api/v2/import/commit/",
        headers=auth,
        json={
            "slot_id": slot["slot_id"],
            "title": "N. 28th and Q Streets",
            "original_date": "9/1956",
            "edtf_date": "1956-09",
            "creator": "Shelton, Edith Keesee",
            "license_name": "CC BY 4.0",
            "source_url": "https://valentine.example/...",
            "description": "35mm color slide...",
            "rotation": 0,
            "mirror": "none",
        },
    )
    resp.raise_for_status()
    print(resp.json())  # {"image_id": 5934}
    ```

=== "R"

    ```r
    library(httr2)

    INSTANCE      <- "https://yesterdays.today"
    TOKEN         <- "your-access-token"
    COLLECTION_ID <- 39
    FILE          <- "/path/to/photo.tif"
    MIME          <- "image/tiff"

    auth <- function(req) req_auth_bearer_token(req, TOKEN)

    # 1. Request an upload slot.
    slot <- request(paste0(INSTANCE, "/api/v2/import/upload-url/")) |>
      auth() |>
      req_url_query(collection = COLLECTION_ID, content_type = MIME) |>
      req_perform() |>
      resp_body_json()

    # 2. PUT the file bytes directly to object storage.
    request(slot$upload_url) |>
      req_method("PUT") |>
      req_headers(!!!slot$upload_headers) |>
      req_body_file(FILE) |>
      req_perform()

    # 3. Commit the import.
    resp <- request(paste0(INSTANCE, "/api/v2/import/commit/")) |>
      auth() |>
      req_body_json(list(
        slot_id       = slot$slot_id,
        title         = "N. 28th and Q Streets",
        original_date = "9/1956",
        edtf_date     = "1956-09",
        creator       = "Shelton, Edith Keesee",
        license_name  = "CC BY 4.0",
        source_url    = "https://valentine.example/...",
        description   = "35mm color slide...",
        rotation      = 0,
        mirror        = "none"
      )) |>
      req_perform() |>
      resp_body_json()
    ```

### Notes

- **Bake transforms in, then upload.** The image bytes are stored as-is. Rotation and mirror are display metadata, not pixel transforms.
- **Processing is asynchronous.** The commit returns as soon as the DB row exists and the file is in its permanent location. Thumbnail, display variant, and IIIF tile generation run in the background — expect a brief window where `thumbnail` and `iiif_url` are null.
- **Atomicity.** Each import is atomic per slot: either an `Image` row is created and the file is in place, or nothing happened. A failure mid-flow leaves the slot consumable for at most the 15-minute window before reaping.
- **Validate license names client-side.** Fetch [`GET /api/v2/licenses/`](licenses.md) once at the start of a batch and check every `license_name` against it. Catching unknown licenses before requesting the upload slot saves a round-trip and a wasted slot.


## Replace an image file

```
POST /api/v2/images/{id}/replace/
```

Replaces the canonical file for an existing image. All of the image's metadata — title, description, date, collection, license, favorites, tags, subjects, georeferences, and comments — remain attached to the same image ID. Only the underlying file bytes change.

This is the endpoint to use when you have a higher-resolution scan of an image that's already in the catalog.

**Requires** an OAuth2 bearer token with the `import` scope, on a user with contributor (staff) status. See [Authentication](authentication.md).

Replacement is a three-step flow:

1. Request an **upload slot** from Yesterdays (returns a presigned URL into our object storage).
2. PUT the file bytes directly to that URL.
3. POST to the replace endpoint with the slot's ID to finalize.

### Step 1: Request an upload slot

```
GET /api/v2/import/upload-url/?collection={id}&content_type={mime}
```

Creates a short-lived import slot and returns a presigned PUT URL into object storage. The slot expires after **15 minutes**; the URL must be used within that window.

| Parameter      | Type    | Required | Description |
|----------------|---------|----------|-------------|
| `collection`   | integer | yes      | Collection ID the target image belongs to. |
| `content_type` | string  | no       | MIME type of the file you will upload. Defaults to `image/jpeg`. Must match exactly when you upload — the presigned URL is signed with this value. |

Example response:

```json
{
    "slot_id": "b5e7c9f2-4a1b-49d3-8c7e-1234567890ab",
    "upload_url": "https://r2.example.com/.../imports/b5e7c9f2...?X-Amz-Signature=...",
    "upload_headers": { "Content-Type": "image/tiff" },
    "cdn_url": "https://cdn.maprva.org/imports/b5e7c9f2..."
}
```

### Step 2: Upload the file

PUT the file bytes to `upload_url` using the exact headers returned in `upload_headers`. Include a `Content-Length` header matching the file size.

The upload target is object storage directly — not Yesterdays. Do not send your Yesterdays access token on this request.

### Step 3: Commit the replacement

```
POST /api/v2/images/{id}/replace/
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `slot_id` | UUID | yes      | The `slot_id` returned in Step 1. |

Example response (200 OK):

```json
{ "image_id": 5934 }
```

### What happens after a successful commit

- `permalink` is updated to point at the newly uploaded file.
- `rotation` and `mirror` reset to `0` and `"none"` — the uploaded file is treated as correctly oriented. Apply any rotation or mirroring *before* uploading.
- A background task regenerates the thumbnail, display variant, and IIIF tiles from the new source, and cleans up prior versions.
- The image stays viewable throughout: `thumbnail`, the display variant, and the IIIF tiles keep serving the *previous* file until each replacement asset is ready, then swap over. Expect a few minutes of the old picture on a large scan.
- The previous `images/{id}/original.*` file is deleted when the new file has a different extension.
- The import slot is consumed (cannot be committed again).

### Errors

| HTTP | Error message                                                       | Meaning                                                                  |
|------|---------------------------------------------------------------------|--------------------------------------------------------------------------|
| 400  | `Slot belongs to a different collection than the target image.`     | The `collection` used to create the slot doesn't match the image's collection. |
| 401  | `invalid_token`                                                     | Access token missing, expired, or revoked.                                |
| 403  | `insufficient_scope` / permission denied                            | Token lacks the `import` scope, or the user is not a contributor.         |
| 404  | `Image not found.`                                                  | No image with the given ID.                                               |
| 404  | `Import slot not found or does not belong to you.`                  | Slot expired, doesn't exist, or was created by a different user.          |
| 409  | `Image has not been uploaded to S3 yet.`                            | No object at the slot's storage key — Step 2 hasn't completed.            |

### Complete example

=== "curl"

    ```bash
    INSTANCE="https://yesterdays.today"
    TOKEN="your-access-token"
    IMAGE_ID=5934
    COLLECTION_ID=39
    FILE=/path/to/replacement.tif
    MIME=image/tiff

    # 1. Request an upload slot.
    SLOT=$(curl -s \
      -H "Authorization: Bearer $TOKEN" \
      "$INSTANCE/api/v2/import/upload-url/?collection=$COLLECTION_ID&content_type=$MIME")
    SLOT_ID=$(echo "$SLOT" | jq -r .slot_id)
    UPLOAD_URL=$(echo "$SLOT" | jq -r .upload_url)

    # 2. PUT the file bytes directly to object storage.
    curl -X PUT "$UPLOAD_URL" \
      -H "Content-Type: $MIME" \
      --data-binary "@$FILE"

    # 3. Commit the replacement.
    curl -X POST "$INSTANCE/api/v2/images/$IMAGE_ID/replace/" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d "{\"slot_id\": \"$SLOT_ID\"}"
    ```

=== "Python"

    ```python
    import requests

    INSTANCE = "https://yesterdays.today"
    TOKEN = "your-access-token"
    IMAGE_ID = 5934
    COLLECTION_ID = 39
    FILE = "/path/to/replacement.tif"
    MIME = "image/tiff"

    auth = {"Authorization": f"Bearer {TOKEN}"}

    # 1. Request an upload slot.
    slot = requests.get(
        f"{INSTANCE}/api/v2/import/upload-url/",
        params={"collection": COLLECTION_ID, "content_type": MIME},
        headers=auth,
    ).json()

    # 2. Stream the file directly to object storage.
    with open(FILE, "rb") as f:
        put = requests.put(
            slot["upload_url"],
            data=f,
            headers=slot["upload_headers"],
        )
        put.raise_for_status()

    # 3. Commit the replacement.
    resp = requests.post(
        f"{INSTANCE}/api/v2/images/{IMAGE_ID}/replace/",
        json={"slot_id": slot["slot_id"]},
        headers=auth,
    )
    resp.raise_for_status()
    print(resp.json())
    ```

=== "R"

    ```r
    library(httr2)

    INSTANCE      <- "https://yesterdays.today"
    TOKEN         <- "your-access-token"
    IMAGE_ID      <- 5934
    COLLECTION_ID <- 39
    FILE          <- "/path/to/replacement.tif"
    MIME          <- "image/tiff"

    auth <- function(req) req_auth_bearer_token(req, TOKEN)

    # 1. Request an upload slot.
    slot <- request(paste0(INSTANCE, "/api/v2/import/upload-url/")) |>
      auth() |>
      req_url_query(collection = COLLECTION_ID, content_type = MIME) |>
      req_perform() |>
      resp_body_json()

    # 2. PUT the file bytes directly to object storage.
    request(slot$upload_url) |>
      req_method("PUT") |>
      req_headers(!!!slot$upload_headers) |>
      req_body_file(FILE) |>
      req_perform()

    # 3. Commit the replacement.
    resp <- request(paste0(INSTANCE, "/api/v2/images/", IMAGE_ID, "/replace/")) |>
      auth() |>
      req_body_json(list(slot_id = slot$slot_id)) |>
      req_perform() |>
      resp_body_json()
    ```

### Notes

- **Orientation.** The replacement file is stored as-is. Rotation and mirror metadata are reset on commit. If your file needs reorientation, bake it into the bytes before uploading.
- **Processing is asynchronous.** The replace call returns as soon as the DB is updated; thumbnail, display variant, and IIIF tile regeneration run in the background. Expect a brief window where `thumbnail` and `iiif_url` are null while derivatives rebuild.
- **Atomicity.** Each replace is atomic per image: the replacement either fully takes effect (DB updated, old file deleted, derivatives queued) or the image is left untouched. There is no partial state.
