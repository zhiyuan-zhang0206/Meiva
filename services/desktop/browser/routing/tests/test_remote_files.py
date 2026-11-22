from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from services.desktop.browser.routing.remote_files import CallerFiles, stage_upload


def test_upload_and_output_paths_never_cross_the_wire(tmp_path: Path) -> None:
    caller = tmp_path / "caller"
    target = tmp_path / "target"
    caller.mkdir()
    target.mkdir()
    (caller / "input.txt").write_bytes(b"upload")
    upload = CallerFiles(caller, "upload_file", {"uid": "1", "filePath": "input.txt"})
    with stage_upload(upload.args, upload.inputs, parent=target) as staged:
        staged_path = Path(str(staged["filePath"]))
        assert staged_path.read_bytes() == b"upload"
        assert staged_path.name == "input.txt"
    assert not staged_path.exists()

    output = CallerFiles(caller, "take_screenshot", {"filePath": "shot.png"})
    result = CallToolResult(content=[TextContent(type="text", text="output.png")])
    received = output.receive(result, {"filePath": [{"path": "output.png", "data": "aW1hZ2U="}]})
    assert (caller / "shot.png").read_bytes() == b"image"
    assert received.content == [TextContent(type="text", text=str(caller / "shot.png"))]


def test_caller_uses_the_format_extension_selected_by_chrome(tmp_path: Path) -> None:
    output = CallerFiles(tmp_path, "take_screenshot", {"filePath": "image.png", "format": "jpeg"})
    result = CallToolResult(content=[TextContent(type="text", text="output.jpg")])
    received = output.receive(
        result, {"filePath": [{"path": "output.jpg", "data": "anBlZyBieXRlcw=="}]}
    )
    assert (tmp_path / "image.jpg").read_bytes() == b"jpeg bytes"
    assert received.content == [TextContent(type="text", text=str(tmp_path / "image.jpg"))]


@pytest.mark.parametrize("name", ["../outside", "symlink/file", "nested/../file"])
def test_remote_artifact_path_confined(tmp_path: Path, name: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "symlink").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="workspace"):
        files = CallerFiles(workspace, "take_screenshot", {"filePath": name})
        result = CallToolResult(content=[])
        files.receive(result, {"filePath": [{"path": "output.png", "data": "eA=="}]})
