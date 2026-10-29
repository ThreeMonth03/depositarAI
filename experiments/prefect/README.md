# Archived Prefect preparation experiment

Status: **failed and incomplete POC**, preserved without fixes.

This experiment was written against source commit
`695070d5a66bddf6022a60da39180c118a52f120`. Its locked dependencies target
CPython 3.13 on Linux x86_64, with Prefect 3.9.0.

The intended flow exercised selected-resource preparation using a synthetic CSV.
Resource HTTP, parser-image identity and extraction were mocked; the proposed
checks covered a retry, cache replay and an offline candidate bundle. It was not
integrated into production deployment or CI.

The recorded run failed in `create_fixture` before those checks completed:

```text
TypeError: write_json() missing 1 required keyword-only argument: 'max_bytes'
```

The calls in `prepare.py` omit that required argument. No successful retry/cache
proof or completed candidate result was produced. The final stdout was empty.
These files archive the attempted implementation, not a passing validation.

Machine-specific paths, raw logs, Prefect state, virtual environments and
download caches are intentionally excluded from this archive commit.
