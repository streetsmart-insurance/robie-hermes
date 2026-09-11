import sys
import pytest
from scripts.prove_deployed import main

def test_prove_deployed_nonexistent_release(tmp_path, capsys):
    nonexistent = tmp_path / "no_such_release"
    code = main(["--release", str(nonexistent)])
    assert code == 2
    captured = capsys.readouterr().out
    assert "release tree does not exist" in captured

def test_prove_deployed_empty_dir_fails(tmp_path, capsys):
    empty = tmp_path / "empty_release"
    empty.mkdir()
    code = main(["--release", str(empty)])
    assert code == 1
    captured = capsys.readouterr().out
    assert "NOT DEPLOYED" in captured
    assert "MISSING" in captured
