from pathlib import Path

from base.telemetry.lgtm_local import service_environment, service_input_paths


def test_service_paths_and_environment(tmp_path: Path) -> None:
    native = tmp_path / "lgtm/native"
    config = native / "config"
    config.mkdir(parents=True)
    (native / "version-loki").write_text("test")
    (native / "platform-loki").write_text("test")
    (config / "loki.yaml").write_text("limits: first")
    paths = service_input_paths(tmp_path, "loki")
    assert len(paths) == 3
    assert service_environment("loki")["GOMEMLIMIT"] == "2GiB"
