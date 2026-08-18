# Public S3 starter catalog

The app imports a **known public catalog manifest**; it does not let a browser
list an S3 bucket or submit arbitrary URLs. The FastAPI service fetches the
configured manifest and objects, verifies their SHA-256 checksums and byte
counts, and copies them into its own case storage. Uploaded cases continue to
work without S3.

## Catalog ownership

Catalog generation and source artwork live outside this deployable repository.
This prevents test data, downloaded labels, annotation material, or source
registry records from leaking into the application image or Git history.

A published catalog uses keys such as
`catalog/cases/<collection>/<beverage-category>/<case-id>/...`. S3 treats the
slashes as key prefixes, so no special folder support is required. Provenance
may accompany source objects but must remain outside the
`verification-request-v1` application payload and can never influence the
comparison.

## Publish scope

Use a dedicated private bucket and publish only the reviewed `catalog/` prefix.
Keep all S3 public-access blocks enabled. Put a public CloudFront distribution
in front of it with origin access control, then apply
`infra/catalog/public-read-policy.json.tftpl` after substituting the exact
bucket name and distribution ARN.

The bucket policy grants only that CloudFront distribution `s3:GetObject` for
`catalog/*`. It intentionally does not grant anonymous S3 access, bucket
listing, write, delete, ACL, or access to any other prefix. Browser CORS is not
needed because the backend, not browser JavaScript, retrieves source files.

Recommended object cache controls:

- `catalog/manifest.json`: `Cache-Control: no-cache`
- `catalog/cases/*`: `Cache-Control: public, max-age=31536000, immutable`

After upload, anonymously fetch the CloudFront manifest URL and verify that
direct anonymous S3 reads and bucket listing are denied. Configure the app
with the exact manifest URL, for example:

```dotenv
VERIFICATION_S3_CATALOG_URL=https://example.cloudfront.net/catalog/manifest.json
```

Only stable catalog assets belong in this public catalog. Do not put customer
uploads, review notes, application credentials, or queue data there.
