# Embedding Service

Every image-embedding feature in Yesterdays runs through one small HTTP service in `services/clip/`:
semantic text search (site and API), similar images, reverse image search, subject similarity, duplicate detection, and the collection statistics behind them.
It holds Meta's [PE-Core L/14@336](https://github.com/facebookresearch/perception_models) as an [OpenVINO](https://docs.openvino.ai/) model and turns text or an image into a unit-length 1024-dimensional vector.
It keeps its historical `clip` name (service, image, and `CLIP_*` environment variables) from the OpenAI CLIP model it served until October 2026.

Django reaches it at `CLIP_SERVICE_URL` through `images/clip_client.py`.
The client checks every response against `settings.EMBEDDING_MODEL_ID` and `settings.EMBEDDING_DIMENSION` and raises `ClipServiceMismatch` on any other model, so vectors from two models never end up in `Image.embedding` together.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /embed/text` | JSON `{"text": "..."}` → `{"embedding": [...], "model": "...", "dim": 1024}` |
| `POST /embed/image` | raw image bytes (`application/octet-stream`) → same shape |
| `GET /info` | bundle metadata, plus `devices`: each tower's execution devices and precision, e.g. `{"text": {"devices": ["GPU"], "precision": "f16"}, …}` |
| `GET /healthz` | liveness: the process is up |
| `GET /readyz` | readiness: the model is loaded and warmed (`503` until then, or if the bundle is missing) |
| `GET /metrics` | Prometheus metrics (see [Metrics](#metrics)) |

Images are squashed (not cropped) to 336×336, as the model was evaluated.
Django sends each image's *displayed* version: the rotated or mirrored copy when one exists, shrunk to at most 1024px on its long side.
`process_image` re-embeds an image whenever that displayed copy appears, changes, or goes away.

!!! note "New images are not embedded automatically"
    Nothing embeds a newly uploaded image on its own.
    Run `manage.py generate_embeddings --queue` after an import, which only picks up images without an embedding.

## Model bundle

The service image doesn't contain PyTorch.
It loads an OpenVINO IR bundle (`text.xml/bin`, `vision.xml/bin`, tokenizer files and `metadata.json`), baked into the release image at build time from a tarball on R2:

```
https://cdn.yesterdays.maprva.org/models/pe-core-l14-336-ir-v1.tar.gz
```

`metadata.json` drives the runtime: model id, dimension, context length, image size, preprocessing (`resize`, `interpolation`, `mean`, `std`) and the tokenizer's special tokens.

### Building a bundle

From `services/clip/`, with the evaluation repository's parity fixtures at hand:

```sh
uv run --group export scripts/export_ir.py \
  --parity ../../../yesterdays-embedding-eval/data/export/parity-v1.json
```

This converts both towers with dynamic batch sizes, saves fp16 weights, writes the bundle into `model/` (which the dev Compose stack mounts), and verifies it before packing:

- the runtime tokenizer produces exactly open_clip's token ids for every parity text,
- the runtime image preprocessing matches open_clip's to within 1e-5,
- OpenVINO on CPU in fp32 matches PyTorch at cosine ≥ 0.999 for both towers.

It then writes `pe-core-l14-336-ir-v1.tar.gz` and prints its sha256.
To publish:

1. Upload the tarball to the R2 bucket at `models/pe-core-l14-336-ir-v1.tar.gz`.
2. Set `MODEL_BUNDLE_URL` and `MODEL_BUNDLE_SHA256` in `.github/workflows/clip-service.yml`.
3. Update the download command in [the contributor guide](../contributing/code.md).

Pushing to `trunk` builds `ghcr.io/maprva/yesterdays-clip:<short sha>`.
The bundle sits in its own image layer, so code-only changes don't re-upload it.

A bundle for a different model also needs `EMBEDDING_MODEL_ID` and `EMBEDDING_DIMENSION` changed in `yesterdays/settings.py`, a migration rebuilding the HNSW indexes at the new dimension (see `images/migrations/0069_pe_core_embeddings.py`), and a cutover like the one below.

### Checking a running service

`scripts/check_service.py` in the evaluation repository posts the parity texts and images to a service and compares the results with open_clip's vectors:

```sh
kubectl -n <namespace> port-forward deploy/yesterdays-clip 8020:8000
uv run scripts/check_service.py --url http://localhost:8020 --precision f16
```

It passes at cosine ≥ 0.999 on CPU fp32, and ≥ 0.998 for text and ≥ 0.995 for images on GPU fp16.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CLIP_MODEL_DIR` | `/app/model` | Where the bundle is |
| `CLIP_TEXT_DEVICE` | `CPU` | OpenVINO device for the text tower: `CPU`, `GPU` or `AUTO` |
| `CLIP_VISION_DEVICE` | `AUTO` | Same, for the vision tower |
| `CLIP_INFERENCE_PRECISION` | empty | `f16` or `f32`; empty picks f16 on GPU and f32 on CPU |
| `CLIP_INFERENCE_THREADS` | empty | Inference threads per tower on CPU, with CPU pinning off; set it to the pod's CPU limit. Empty leaves it to OpenVINO, which sizes its pool from the visible CPUs and ignores the limit |
| `CLIP_CACHE_DIR` | empty | Compiled-kernel cache (hardware specific; keep it on an emptyDir) |
| `CLIP_MAX_IMAGE_BYTES` | 20 MiB | Largest accepted `/embed/image` body |

The Helm chart exposes these as `clip.textDevice`, `clip.visionDevice` and `clip.inferencePrecision`.
`clip.strategy` (default `Recreate`) keeps a rollout from running two pods at once, and `clip.gpu.*` requests an Intel GPU.

fp16 costs nothing measurable in search quality (≤ 0.2 points on every benchmark in the October 2026 evaluation).
Set the precision explicitly on CPU: OpenVINO otherwise defaults to bf16 on CPUs that support it.

### Deployments

| | Text | Vision | Precision | GPU |
|---|---|---|---|---|
| Production | `CPU` | `CPU` | `f32` | none |
| Staging | `CPU` | `CPU` | `f32` | none |
| Development (Compose) | `CPU` | `CPU` | auto (f32) | none |

Production runs on CPU because the Arc A310's memory silently corrupts over time, which degrades the model's weights while it is loaded.

GPU pods need the `gpu.intel.com/i915` resource and the node's `render` group as a supplemental group (`clip.gpu.renderGroupId`).

### Memory and VRAM

The bundle stores fp16 weights (about 1.25 GiB).
On CPU in fp32, OpenVINO expands them in host memory: the service measured about 4.9 GiB resident (3.5 GiB anonymous plus 1.35 GiB of memory-mapped weight files), which is why the chart requests 5 GiB and limits the pod to 7 GiB.
On the GPU in fp16 both towers fit in about 1.25 GiB of VRAM plus activations.
The A310 is shared between pods with no memory isolation (`sharedDevNum` in the fluxcd `gpu-plugin.yaml`).
Its budget is recorded there: keep it current when GPU usage changes.

## Metrics

The service serves Prometheus metrics at `GET /metrics` on its HTTP port; scrape it with a `ServiceMonitor` alongside the app's.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `clip_requests_total` | counter | `endpoint`, `status` | `/embed/text` and `/embed/image` requests by response status (probes aren't counted) |
| `clip_request_duration_seconds` | histogram | `endpoint` | Request latency inside the service |
| `clip_phase_duration_seconds` | histogram | `tower`, `phase` | Time per encoding phase: `decode` and `preprocess` (vision) or `tokenize` (text), then `lock_wait` and `infer` |
| `clip_inflight_requests` | gauge | `tower` | Requests being encoded, including those queued on the tower's lock |
| `clip_image_bytes` | histogram | | Size of `/embed/image` bodies |
| `clip_text_tokens` | histogram | | BPE tokens per text query, before truncation |
| `clip_text_truncated_total` | counter | | Text queries cut off at the context length (32 tokens for PE-Core, 30 of them text) |
| `clip_model_state` | enum | `clip_model_state` | `loading`, `ready` or `failed` |
| `clip_model_load_seconds` | gauge | | Time to compile and warm up the model: long when the kernel cache was cold |
| `clip_model_info` | info | `model`, `{text,vision}_devices`, `{text,vision}_precision` | The loaded model and where each tower actually runs |

Each tower has a single infer request, so concurrent requests queue: `lock_wait` growing while `infer` holds steady means the service is saturated (typically a `generate_embeddings` run competing with live searches), not that inference got slower.
Warmup inference is not recorded, so a restart doesn't drop a cold first inference into the latency histograms.

Django records its side of every call in `clip_client_requests_total` (labels `operation` = `text`/`image`, `outcome` = `ok`, `timeout`, `connection_error`, `http_<status>`, `mismatch` or `error`) and `clip_client_request_duration_seconds`.
These still report when the service is down or unreachable.
Only the web pods expose them: calls made inside Celery workers aren't scraped, so embedding tasks are visible through the service's own metrics only.

## Moving vectors in and out

`export_embeddings` and `import_embeddings` use the same `.npz` format: `ids` (int64), `vectors` (one row per id, float32 or float16), `meta` (a JSON string with at least `model_id`) and, optionally, `urls` (each image's displayed permalink when its vector was computed).

```sh
manage.py export_embeddings /tmp/embeddings.npz [--dtype float16] [--limit N] [--model-id ID]
manage.py import_embeddings /tmp/embeddings.npz [--refresh-stats]
manage.py import_embeddings https://example.com/embeddings.npz
```

`import_embeddings` rejects a file unless its model id and dimension match the settings and every vector is unit length.
It only fills rows whose embedding is still empty, so re-running it never overwrites vectors that `generate_embeddings` has since replaced.
When the file has `urls`, it also skips any image whose displayed permalink no longer matches, so ids that name different images in this database never receive another image's vector.
It reports how many ids it wrote, skipped as already embedded, skipped as differing, and didn't recognise.
`--model-id` and `--dimension` override the expected values, for restoring a backup from another model during a rollback.

## Cutover runbook: CLIP to PE-Core (October 2026)

Do staging first, then production.
Run Django commands in the import pod (`kubectl -n <namespace> exec -it deploy/yesterdays-import -- /app/.venv/bin/python manage.py …`), which can reach both the database and the embedding service.
From the migration until the seed import finishes (around 15 minutes), semantic search, similar images, reverse search and subject similarity return nothing.

### 1. Back up the CLIP vectors

The running image predates `export_embeddings`, so pipe the command's source into `manage.py shell` there:

```sh
NS=georeference-tool-prod-3
( cat images/management/commands/export_embeddings.py
  echo 'Command().run_from_argv(["manage.py", "export_embeddings", "/tmp/clip-l14-336-backup.npz", "--model-id", "openai/clip-vit-large-patch14-336"])'
) | kubectl -n $NS exec -i deploy/yesterdays-import -- /app/.venv/bin/python manage.py shell
kubectl -n $NS cp "$(kubectl -n $NS get pod -l app=yesterdays-import -o name | head -1 | cut -d/ -f2)":/tmp/clip-l14-336-backup.npz ./clip-l14-336-backup.npz
```

Keep the file until the switch has settled.

### 2. Deploy

Roll out the chart release carrying the new app image and the PE-Core `yesterdays-clip` image.
The pre-upgrade migration job applies `images.0069_pe_core_embeddings`:

- drops both HNSW indexes,
- replaces `Image.embedding` with an empty column,
- empties `CollectionEmbeddingStats` and `DuplicateImagePair` (dismissed duplicate pairs are kept),
- builds new `vector(1024)` HNSW indexes on the empty column.

### 3. Seed

Load the vectors computed in the evaluation repository (`data/export/pe-core-l14-336-seed-v1.npz`, about 110 MB: float16 vectors plus each image's displayed URL):

```sh
POD=$(kubectl -n $NS get pod -l app=yesterdays-import -o name | head -1 | cut -d/ -f2)
kubectl -n $NS cp pe-core-l14-336-seed-v1.npz $POD:/tmp/pe-core-l14-336-seed-v1.npz
kubectl -n $NS exec -it $POD -- /app/.venv/bin/python manage.py import_embeddings /tmp/pe-core-l14-336-seed-v1.npz --refresh-stats
```

The seed holds production image ids, but an image is only filled while its displayed URL still matches the one the vector was computed from. Where the ids name other images (a staging or dev database that has diverged from production) or an image has been re-transformed since the snapshot, those rows are reported as differing and left empty; regeneration (step 5) covers them.

### 4. Smoke test

- `GET /info` on the service (port-forward) reports `PE-Core-L-14-336`, dim 1024, and on production `GPU` with `f16` for both towers.
- `check_service.py` passes (see [above](#checking-a-running-service)).
- On the site: a semantic search, an image's similar page, a reverse image search, and a subject's similar page.

### 5. Regenerate server-side

```sh
manage.py generate_embeddings --force --queue
```

This re-embeds every image through the service on the background queue, one request at a time, while search keeps working on the seed vectors.
Measure its rate on staging first.

### 6. Afterwards

- `manage.py shell -c "from images.tasks import refresh_collection_embedding_stats; refresh_collection_embedding_stats()"`, or wait for the daily refresh.
- Let the nightly duplicate scan rebuild its pairs (or run `images.tasks.refresh_duplicate_image_pairs` once).
- Check that regeneration matches the seed's pipeline: `export_embeddings /tmp/sample.npz --limit 2000` and compare against the seed by id. Expect a median cosine around 0.998, with a tail down to about 0.93: the seed was embedded from reduced-scale decodes of the 1920px JPEGs, the service from the client's 1024px PNG. A much lower median means the pipeline differs (for example, images being cropped again).

### Rollback

Migration 0069 only exists in the new image, so reverse it before reverting the chart:

1. In the new image: `manage.py migrate images 0068`. This restores the 768-dimension indexes on an empty column.
2. Revert the chart to the previous app and `yesterdays-clip` images.
3. Restore the CLIP vectors. The old image predates `import_embeddings`, so pipe it in as in step 1, with the backup copied to `/tmp` in the import pod:

    ```sh
    ( cat images/management/commands/import_embeddings.py
      echo 'Command().run_from_argv(["manage.py", "import_embeddings", "/tmp/clip-l14-336-backup.npz", "--model-id", "openai/clip-vit-large-patch14-336", "--dimension", "768", "--refresh-stats"])'
    ) | kubectl -n $NS exec -i deploy/yesterdays-import -- /app/.venv/bin/python manage.py shell
    ```
