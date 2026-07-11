# Contributing

Contributions are welcome through issues and pull requests.

## Local checks

The test suite has no third-party test dependencies:

```sh
python3 -m unittest discover -s lambda -p 'test_*.py' -v
node cloudfront-function/test_handler_logic.js
```

To build the Lambda deployment archive, run:

```sh
./scripts/build-lambda.sh
```

The generated `package/` directory and `htaccess_bridge.zip` archive are local
build artifacts and must not be committed.

## Pull requests

- Keep the supported `.htaccess` subset intentionally small and explicit.
- Add tests for behavior changes.
- Update both Japanese and English documentation when public behavior changes.
- Do not include AWS account IDs, credentials, secret values, or deployment artifacts.
