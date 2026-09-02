"""Local pytest config for data/tests.

Registers the ``network`` marker used by
test_binance_vision_um.py.  Network tests are additionally guarded by
an env-var skipif so they stay off by default even when the suite is
run without ``-m "not network"``.
"""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: hits data.binance.vision; opt in with "
        "BINANCE_UM_NETWORK_TESTS=1",
    )
