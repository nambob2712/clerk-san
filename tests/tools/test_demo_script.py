from __future__ import annotations

from scripts.demo_local import _prepare_output


def test_demo_output_requires_explicit_reset_before_replacement(tmp_path) -> None:
    output = tmp_path / "demo"
    assert _prepare_output(output, reset=False) == output.resolve()
    (output / "evidence.txt").write_text("preserve", encoding="utf-8")

    try:
        _prepare_output(output, reset=False)
    except SystemExit as error:
        assert "--reset" in str(error)
    else:  # pragma: no cover - protects the destructive-demo contract
        raise AssertionError("expected a non-empty demo directory to require --reset")

    assert _prepare_output(output, reset=True) == output.resolve()
    assert not (output / "evidence.txt").exists()
