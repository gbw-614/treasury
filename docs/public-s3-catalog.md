# Public catalog integration

The application imports cases from one configured, read-only catalog manifest.
It does not let browsers list a bucket or submit arbitrary URLs. FastAPI fetches
the manifest and its referenced objects, verifies SHA-256 checksums and byte
counts, then snapshots accepted JSON and artwork into local case storage.
Uploaded cases do not depend on the catalog.

## Configure the runtime

Serve a manifest and its objects through HTTPS, then configure its exact URL:

```dotenv
VERIFICATION_S3_CATALOG_URL=https://catalog.example.com/catalog/manifest.json
```

The URL is optional. When it is blank, the catalog-import action is hidden and
manual/single-file/batch uploads continue to work.

## Manifest contract

The service accepts the versioned catalog contract implemented by
`backend/app/schemas/sources.py`. A catalog entry supplies a stable source-case
ID, a verification-request JSON object, and one or more ordered artwork
objects. New entries should use `verification-request-v2` and declare the
field-library version used to create them.

Referenced objects must remain on the manifest's HTTPS origin and below its
directory prefix. Redirects, off-origin objects, invalid hashes, unexpected
media types, oversized data, duplicate panel IDs, and invalid request schemas
are rejected before a queue case is created.

## Hosting boundary

Publish only stable reference cases and label artwork. Keep uploads, reviewer
notes, accounts, queue data, credentials, and application storage out of the
catalog. A private S3 bucket behind CloudFront with origin access control is a
good pattern: grant the distribution `s3:GetObject` only for the `catalog/*`
prefix, with no bucket-list, write, delete, or anonymous S3 permissions.

Suggested cache policy:

- `catalog/manifest.json`: `Cache-Control: no-cache`
- referenced case JSON and artwork: `Cache-Control: public, max-age=31536000, immutable`

The backend—not browser JavaScript—retrieves catalog objects, so browser CORS
is not required.
