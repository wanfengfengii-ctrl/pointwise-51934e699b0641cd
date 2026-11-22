"""Run this project's full unittest suite from the repository root."""
import sys
import unittest


def _run_fixture_checks():
    """Exercise the four-station offset fixture end to end.

    Returns ``True`` when every assertion holds; any failure prints and returns
    ``False`` so the caller can exit non-zero.
    """
    try:
        import offset_fixture
        offset_fixture.check_base()
        offset_fixture.check_offsets()
    except Exception as exc:  # noqa: BLE001 - surface any fixture failure as a check failure
        print(f'FIXTURE FAIL {type(exc).__name__}: {exc}', file=sys.stderr)
        return False
    print('FIXTURE SUMMARY result=PASS')
    return True


def main():
    suite = unittest.defaultTestLoader.discover('tests', top_level_dir='.')
    if suite.countTestCases():
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    else:
        print('No tests discovered in tests/', file=sys.stderr)
        result = unittest.TestResult()
    for status, failed_cases in (
        ('FAIL', (test for test, _ in result.failures)),
        ('ERROR', (test for test, _ in result.errors)),
        ('UNEXPECTED_SUCCESS', result.unexpectedSuccesses),
    ):
        for test in failed_cases:
            print(f'{status} {test.id()}')
    successful = result.testsRun > 0 and result.wasSuccessful()
    print(f'TEST SUMMARY total={result.testsRun} failures={len(result.failures)} '
          f'errors={len(result.errors)} skipped={len(result.skipped)} '
          f'expected_failures={len(result.expectedFailures)} '
          f'unexpected_successes={len(result.unexpectedSuccesses)} '
          f'result={"PASS" if successful else "FAIL"}')
    successful = _run_fixture_checks() and successful
    return 0 if successful else 1


if __name__ == '__main__':
    raise SystemExit(main())
