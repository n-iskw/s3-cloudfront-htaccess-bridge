# ADR 0001: Normalize Cache-Control after S3 uploads

- Status: Accepted
- Date: 2026-08-30
- Issue: [#10](https://github.com/n-iskw/s3-cloudfront-htaccess-bridge/issues/10)

## Context

CloudFront's managed `CachingOptimized` policy uses a 24-hour default TTL when
an origin response has no cache metadata. It honors an origin
`Cache-Control: max-age` value within the policy's minimum and maximum TTLs.
Content creators can upload through several S3 clients, so requiring every
client to supply the header is not a dependable site-wide default.

Routine CloudFront invalidations are intentionally not part of this workflow.
The desired default is a short cache that still lets CloudFront absorb traffic
bursts and checks S3 again after about one minute.

## Decision

The existing S3-event Lambda also normalizes cache metadata for ordinary
content uploads:

- `DEFAULT_CACHE_CONTROL` is configurable and defaults to
  `public, max-age=60`.
- An uploader-supplied `Cache-Control` value is preserved.
- The Lambda handles direct `Put`, `Post`, and `CompleteMultipartUpload`
  creation events. Content `Copy` events are outside this default event route
  so the Lambda's own same-key copy cannot recursively invoke itself.
- `.htaccess`, `.htpasswd`, and bridge history objects remain control data and
  do not receive content cache metadata.
- Because S3 has no metadata-only update operation, the Lambda performs a
  conditional same-key `CopyObject` with `MetadataDirective=REPLACE` while
  carrying forward user metadata and relevant system metadata.
- Both source and destination ETag preconditions are used. A version ID from
  the event is also checked against the current object and used as the copy
  source. Detectably stale events and failed preconditions are skipped instead
  of making old content current. A retryable S3 `409
  ConditionalRequestConflict` is raised so asynchronous Lambda delivery can
  retry it.
- Object tags are copied. S3 object annotations are excluded to avoid adding
  annotation-specific permissions and per-annotation copy requests.

S3 notification configuration uses one unfiltered rule for the three content
creation event types. Separate `.htaccess` and `.htpasswd` suffix rules cover
`Copy` and removal events. These event types do not overlap, so S3 accepts the
configuration and the metadata copy does not re-enter the content route.

## Consequences

- Processing is asynchronous. A CloudFront miss that reaches S3 before the
  Lambda copy completes can cache the headerless object using the managed
  policy's default TTL. This mechanism reduces routine invalidations but is
  not an exact 60-second publication guarantee.
- ETags identify content, not metadata. A concurrent upload of identical bytes
  with different metadata can retain the same ETag and evade the conditional
  race check. This narrow race remains best-effort.
- A same-key copy changes `Last-Modified`; with S3 Versioning it creates a new
  current version. With Versioning suspended, it replaces the current null
  version and uses ETag checks as best-effort concurrency protection. The copy
  also emits a real `ObjectCreated:Copy` event, so unrelated EventBridge,
  notification, replication, or audit consumers must tolerate or exclude this
  metadata-only bridge copy. It also applies S3's normal copy ACL behavior, so
  this reference implementation assumes bucket-owner-enforced ownership and a
  private S3 origin accessed through CloudFront OAC/OAI rather than legacy
  public object ACLs.
- Atomic `CopyObject` supports source objects up to 5 GiB. Larger objects are
  logged and left unchanged; the 30-second Lambda timeout can also prevent a
  large copy below that API limit from completing. Multipart metadata
  replacement is outside this bridge's static-site scope.
- Customer-managed SSE-KMS keys require the deployment to grant the Lambda
  role the appropriate KMS decrypt and data-key permissions.
- Objects with Object Lock retention/legal-hold settings are detected with
  read-only permissions and skipped. SSE-C is unsupported because the copy
  cannot supply the customer-provided encryption key.
- An existing unfiltered or overlapping S3 notification for the same creation
  event types cannot coexist with this rule. Deployments must consolidate
  dispatch or use EventBridge instead of overwriting existing notification
  ownership.
- The direct S3 notification and Lambda must be in the same AWS Region.

## Alternatives considered

- **Require upload clients to set Cache-Control:** most efficient and avoids
  the asynchronous race, but cannot enforce a consistent default across the
  supported arbitrary-client workflow.
- **Use a custom CloudFront cache policy:** a good standard-plan option, but it
  does not satisfy the flat-rate managed-policy constraint motivating this
  feature.
- **Use the managed `UseOriginCacheControlHeaders` policy:** this can honor the
  same origin header without `CachingOptimized`'s 24-hour fallback, but its
  cache key includes all cookies plus `Host` and `Origin`, which can reduce the
  cache hit ratio. Plan availability must also be confirmed; it does not remove
  the need to set a short origin header on uploaded objects.
- **Invalidate on every upload:** provides an explicit cache purge but adds an
  operational step that this feature is intended to remove.
- **Add an origin-response edge function:** adds another runtime and cost path
  even though S3 object metadata already supplies the header CloudFront needs.

## References

- [CloudFront managed cache policies](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-cache-policies.html)
- [CloudFront cache expiration](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/Expiration.html)
- [S3 CopyObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CopyObject.html)
- [S3 event notification filtering](https://docs.aws.amazon.com/AmazonS3/latest/userguide/notification-how-to-filtering.html)
