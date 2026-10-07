import lzma
import struct

import pandas as pd

from goldlab.dukascopy import REC, _decode, _scale


def test_decode_and_scale_bi5_candles():
    recs = [(0, 2063500, 2064100, 2063200, 2064500, 1.5), (60, 2064100, 2064000, 2063900, 2064300, 0.0),
            (120, 2064000, 2065000, 2063800, 2065200, 2.0)]
    blob = lzma.compress(b"".join(REC.pack(*r) for r in recs), format=lzma.FORMAT_ALONE)
    day = pd.Timestamp("2024-01-09", tz="UTC")
    df = _scale(_decode(blob, day))
    assert len(df) == 2  # zero-volume padding minute dropped
    assert df.index[1] == day + pd.Timedelta(minutes=2)
    row = df.iloc[0]
    assert (row.open, row.close, row.low, row.high) == (2063.5, 2064.1, 2063.2, 2064.5)
    assert struct.calcsize(">5If") == 24
