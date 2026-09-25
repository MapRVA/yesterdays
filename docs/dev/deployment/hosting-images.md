# Hosting Images

Yesterdays is designed to store images and other assets in an S3-compatible bucket.
For [Yesterdays of Richmond](https://yesterdays.today), `cdn.yesterdays.maprva.org` is pointed at this bucket.

In general we recommend [Cloudflare R2](https://www.cloudflare.com/developer-platform/products/r2/) for its low fees and ease of use.
However, you can use any S3-compatible service that you wish.
You will need to configure your bucket's CORS to allow requests from your Yesterdays instance's domain.
Instructions for configuring your Yesterdays instance to use the bucket is [here](/deployment/helm-chart/).
