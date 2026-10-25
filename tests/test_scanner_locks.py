"""itila2 scanner accessors are safe while another thread feeds."""
import ctypes as ct
import os
import threading

import numpy as np
import pytest
from test_rtty_cw_exclusion import SC_LIB, _feed, _scanner

pytestmark = pytest.mark.skipif(not os.path.exists(SC_LIB), reason="libitila2_scanner.so not built")


def test_draining_while_another_thread_feeds() -> None:
    lib, h = _scanner()
    lib.itila_sc_env_n.restype = ct.c_int
    lib.itila_sc_env_n.argtypes = [ct.c_void_p, ct.c_double]
    lib.itila_sc_drain_env.restype = ct.c_int
    lib.itila_sc_drain_env.argtypes = [ct.c_void_p, ct.c_double, ct.POINTER(ct.c_double),
                                       ct.POINTER(ct.c_double), ct.c_int]
    stop = threading.Event()
    seen = []

    def drain() -> None:
        e100 = np.zeros(4000)
        e200 = np.zeros(4000)
        p100 = e100.ctypes.data_as(ct.POINTER(ct.c_double))
        p200 = e200.ctypes.data_as(ct.POINTER(ct.c_double))
        buf = (ct.c_double * 64)()
        while not stop.is_set():
            for i in range(lib.itila_sc_list_bins(h, buf, 64)):
                n = lib.itila_sc_env_n(h, buf[i])
                seen.append(n >= 0 and lib.itila_sc_drain_env(h, buf[i], p100, p200, 4000) <= 4000)

    t = threading.Thread(target=drain)
    try:
        t.start()
        _feed(lib, h, 4.0, [5000.0, 12000.0])
    finally:
        stop.set()
        t.join()
        lib.itila_sc_free(h)
    assert seen and all(seen)
