from scripts.demo import run_demo


def test_real_http_demo_preserves_stock_and_history():
    summary = run_demo()
    assert summary["http_demo"] == "passed"
    assert summary["audit"]["ok"] is True
    assert summary["stock_conflicts"] == 11
