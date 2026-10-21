from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from app.core.login_limits import LoginRateLimiter
from app.services.errors import ServiceError


@dataclass
class Clock:
    now: float = 100

    def __call__(self) -> float:
        return self.now


def assert_rejected(operation, code, status):
    with pytest.raises(ServiceError) as caught:
        operation()
    assert (caught.value.code, caught.value.status_code) == (code, status)


def test_ip_and_email_have_independent_limits_and_normalized_identity():
    limiter = LoginRateLimiter(ip_limit=2, email_limit=1)
    limiter.check_ip("192.0.2.1")
    limiter.check_ip("192.0.2.1")
    assert_rejected(lambda: limiter.check_ip("192.0.2.1"), "LOGIN_RATE_LIMITED", 429)
    limiter.check_ip("192.0.2.2")
    limiter.check_email("User@Example.test")
    assert_rejected(lambda: limiter.check_email(" user@example.test "), "LOGIN_RATE_LIMITED", 429)
    limiter.check_email("different@example.test")
    # Even equal input text has distinct buckets in the two namespaces.
    limiter.check_email("192.0.2.1")


def test_rejections_do_not_slide_window_and_boundary_resets_counters():
    clock = Clock()
    limiter = LoginRateLimiter(ip_limit=1, clock=clock)
    limiter.check_ip("192.0.2.1")
    clock.now = 159.999
    assert_rejected(lambda: limiter.check_ip("192.0.2.1"), "LOGIN_RATE_LIMITED", 429)
    clock.now = 160
    limiter.check_ip("192.0.2.1")
    assert_rejected(lambda: limiter.check_ip("192.0.2.1"), "LOGIN_RATE_LIMITED", 429)


def test_capacity_fails_closed_without_evicting_live_counter():
    clock = Clock()
    limiter = LoginRateLimiter(ip_limit=1, max_keys=2, clock=clock)
    limiter.check_ip("192.0.2.1")
    clock.now += 1
    limiter.check_email("private@example.test")
    assert_rejected(lambda: limiter.check_ip("192.0.2.2"), "AUTH_CAPACITY_EXCEEDED", 503)
    assert len(limiter._buckets) == 2
    assert_rejected(lambda: limiter.check_ip("192.0.2.1"), "LOGIN_RATE_LIMITED", 429)
    # Expiring only the first entry permits admission without resetting the second.
    clock.now = 160
    limiter.check_ip("192.0.2.2")
    assert len(limiter._buckets) == 2
    assert_rejected(
        lambda: limiter.check_email("another@example.test"), "AUTH_CAPACITY_EXCEEDED", 503
    )
    clock.now = 161
    limiter.check_email("another@example.test")
    assert len(limiter._buckets) == 2


def test_stored_keys_do_not_contain_raw_addresses():
    limiter = LoginRateLimiter()
    limiter.check_ip("192.0.2.23")
    limiter.check_email("private@example.test")
    assert all(isinstance(key, bytes) and len(key) == 32 for key in limiter._buckets)
    assert "private@example.test" not in repr(limiter.__dict__)
    assert "192.0.2.23" not in repr(limiter.__dict__)


def test_concurrent_threads_cannot_overrun_limit():
    limiter = LoginRateLimiter(ip_limit=7)

    def attempt(_):
        try:
            limiter.check_ip("192.0.2.1")
            return True
        except ServiceError as error:
            assert (error.code, error.status_code) == ("LOGIN_RATE_LIMITED", 429)
            return False

    with ThreadPoolExecutor(max_workers=16) as executor:
        accepted = list(executor.map(attempt, range(64)))
    assert sum(accepted) == 7
    assert len(limiter._buckets) == 1


def test_clock_regression_does_not_reopen_window_or_reorder_expiry():
    clock = Clock()
    limiter = LoginRateLimiter(ip_limit=1, max_keys=2, clock=clock)
    limiter.check_ip("192.0.2.1")
    clock.now = 90
    limiter.check_ip("192.0.2.2")
    clock.now = 150
    assert_rejected(lambda: limiter.check_ip("192.0.2.2"), "LOGIN_RATE_LIMITED", 429)
    clock.now = 160
    limiter.check_ip("192.0.2.2")
    limiter.check_ip("192.0.2.1")


@pytest.mark.parametrize(
    "values",
    [
        {"ip_limit": 0},
        {"email_limit": -1},
        {"max_keys": 0},
        {"window_seconds": 0},
        {"window_seconds": -1},
        {"window_seconds": float("inf")},
        {"window_seconds": float("nan")},
    ],
)
def test_invalid_limits_are_rejected(values):
    with pytest.raises(ValueError):
        LoginRateLimiter(**values)
