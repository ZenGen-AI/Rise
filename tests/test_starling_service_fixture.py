"""真 starling-serve 夹具本身的检查:连得上、报钉住的提交、不把 token 写进日志、不往 HOME 里写。"""
import pytest

from starling_service import start_service


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    proc = start_service(tmp_path_factory.mktemp("svc"))
    yield proc
    proc.kill()


def test_the_service_reports_the_pinned_commit_and_never_logs_its_token(service):
    import starling_client
    client = starling_client.connect(service.url, token=service.token)
    try:
        info = client.info()
    finally:
        client.close()
    assert info["commit"] == starling_client.COMMIT == "cff319d952360727823024ff43c71b15055bd8e3"
    assert info["protocol"] == starling_client.PROTOCOL
    assert service.token not in service.stderr_path.read_text("utf-8", "replace")


def test_the_service_home_stays_empty(service):
    assert not any((service.data_dir.parent / "home").iterdir())     # 不碰真 HOME,也不往假 HOME 里写
