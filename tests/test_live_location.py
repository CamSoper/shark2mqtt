from src.main import _floor_file_updated_at


def test_floor_file_updated_at_reads_shadow_file_list():
    raw = {
        "shadow": {
            "properties": {
                "reported": {
                    "fileList": {
                        "Visual_Floor_1": {
                            "value": {"fileName": "floorRPfile1-1788841534.bin"},
                            "updatedAt": "2026-09-08T04:25:36.000Z",
                        }
                    }
                }
            }
        }
    }
    assert _floor_file_updated_at(raw) == "2026-09-08T04:25:36.000Z"

def test_floor_file_updated_at_missing_entries():
    assert _floor_file_updated_at({}) == ""
    assert _floor_file_updated_at({"shadow": {}}) == ""
    assert _floor_file_updated_at(
        {"shadow": {"properties": {"reported": {"fileList": {}}}}}
    ) == ""