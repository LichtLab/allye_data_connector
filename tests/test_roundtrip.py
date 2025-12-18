import pandas as pd

import allye_data_connector as adc


def test_roundtrip_file(tmp_path):
    df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    name = adc.send_dataframe(df, table_name="t1", secret_dir=tmp_path, transport="file", chunk_rows=2)
    out = adc.get_dataframe(name, secret_dir=tmp_path)
    pd.testing.assert_frame_equal(out, df)
