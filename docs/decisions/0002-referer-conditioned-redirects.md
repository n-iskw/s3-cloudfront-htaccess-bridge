# ADR 0002: Referer-conditioned redirects and denial in the bridge

- Status: Proposed
- Date: 2026-10-19
- Updated: 2026-11-01 (extend the open proposal with forbidden rules)

## Context and drivers

Content editors need to discourage direct access to selected pages and choose
an error destination for each rule. Conditions should survive bridge upgrades
and remain editable through the existing `.htaccess` upload workflow. A cache
behavior has only one viewer-request function association, already owned by
the bridge. CloudFront custom error responses do not process errors generated
by that function. Error presentation should remain with CloudFront while the
Bridge keeps the Apache-like condition and denial syntax.

## Decision

Extend the upstream parser and the same viewer-request function with the
limited syntax `RewriteCond %{HTTP_REFERER} [!]regex [NC]`. Consecutive
conditions are ANDed onto the immediately following redirect or forbidden RewriteRule.
Accept substitution `-` with bare `[F]` or `[F,L]` as terminal 403 denial.
Keep conditions as optional fields in existing redirect rules and KVS chunks;
unconditional rules retain their behavior. Preserve regex backslashes when
tokenizing RewriteCond and RewriteRule. Reject other variables, condition
flags, dangling conditions and unsupported regex constructs before publishing.

The function evaluates conditions after existing access checks and before
DirectoryIndex processing and cache lookup. For forbidden GET/HEAD requests,
rewrite only the origin URI to the common reserved absent object
`/_htaccess-bridge/forbidden.html`, clear the query string, and stop rule/index
processing. Integrated S3 REST origins keep that namespace empty and do not
grant the CloudFront S3 principal ListBucket, so the request produces origin
403. Existing CloudFront error responses own its page, status, and caching.
This uses neither site-specific Bridge settings nor an extra S3 deny policy.

Other methods receive a direct empty 403 with `Cache-Control: no-store`; the
Bridge must not send a denied write or CORS preflight to the reserved key.
Conditional redirects have `Cache-Control: no-store`. Page URLs and error
destinations stay in site content or CloudFront settings; no `ErrorDocument`
emulation or Distribution updates are part of the Bridge.

## Alternatives considered

- Manual Function edits: overwritten by the next source-managed deployment.
- Infrastructure-only site settings composed with the bridge: require an
  infrastructure update whenever content editors change their navigation.
- S3 bucket conditions: require origin header forwarding and cache handling;
  add policy and cache coordination outside the existing upload workflow.
- Direct edge 403 for GET/HEAD: simpler, but bypasses CloudFront error pages.
- A dedicated S3 deny policy: guarantees denial if an object appears, but is
  unnecessary under the agreed empty-namespace and no-ListBucket contract.
- ErrorDocument emulation: duplicates CloudFront's page/status handling and
  adds content synchronization responsibilities to the Bridge.
- Full Apache RewriteCond emulation: includes server/filesystem state that is
  unavailable to a viewer-request Function and exceeds this feature's scope.

## Consequences and limits

- Referer is optional, policy-dependent and forgeable: this is navigation
  control, not authentication. Missing headers are evaluated as an empty string.
- The reserved namespace must remain empty in each applicable origin,
  including any OriginPath prefix. If an object is placed there, it could be
  served instead of denied. S3 website and non-S3 origins are unsupported for
  this denial mechanism; ListBucket grants change missing-key 403 to 404.
- The viewer URL stays unchanged for `[F]`. Final status and error caching
  follow CloudFront settings, so a configured 403-to-404 mapping differs from
  Apache's default final 403. No error page or status mapping is hardcoded.
- Changing the URI does not change the chosen cache behavior or origin.
  The rewrite precedes cache lookup, keeping denied requests separate from
  cached allowed content. The contract must hold for every applicable origin.
- Existing IP-only denial, blocked control files, and unsafe redirects retain
  their direct edge responses. Only the new `[F]` path uses origin errors.
- Redirecting to an uploaded error page changes the URL and normally ends in
  a 200 response. `ErrorDocument` remains unsupported.
- Regexes are limited to 200 characters, literal/punctuation escapes, ordinary
  or non-capturing groups, classes, alternation and non-possessive quantifiers.
  Inline modes, lookarounds, alphanumeric escapes and condition captures are
  unsupported. Redirect loop detection remains conservative and ignores
  conditions when probing possible chains.
- Existing rule specificity ordering is retained. Rules with the same path
  pattern and different conditions keep their declaration order. Conditions
  reset after their RewriteRule. Overlapping URL aliases must be covered by
  configuration because matching precedes DirectoryIndex rewriting.
- Consumers must adopt the parser and Function from the same release before
  uploading the new syntax. A pinned consumer release is not automatically
  upgraded. No production adoption is included here.

## Implementation and validation

This is a proposed implementation pending review and merge. Parser validation,
KVS packing and last-good-config retention are tested in Python. The Node
tests send actual Python-generated KVS data through both source and minified
Functions, including per-page destinations, absent and mismatching Referer,
AND/NC conditions, nested scopes, authentication priority and legacy behavior.
Tests also check reserved-key routing, query removal, HEAD method preservation,
and direct rejection of other methods without sending them to S3.
These local tests do not establish deployed CloudFront runtime behavior.

## References

- [Apache RewriteCond](https://httpd.apache.org/docs/2.4/mod/mod_rewrite.html#rewritecond)
- [CloudFront event structure](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/functions-event-structure.html)
- [Viewer-request timing](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/function-code-choose-purpose.html)
- [S3 missing-key response](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html)
