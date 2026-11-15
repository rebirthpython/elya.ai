from services.code_stats import count_code_lines


def test_counts_source_lines_and_skips_environment(tmp_path):
    (tmp_path / "main.py").write_text("a = 1\nb = 2\n")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "app.js").write_text("x()\ny()\nz()\n")
    (tmp_path / "README.md").write_text("# Эля\n")
    (tmp_path / "photo.png").write_bytes(b"\x89PNG\n\n\n")  # не код
    for skipped in (".venv/lib", ".git", "__pycache__", "data"):
        folder = tmp_path / skipped
        folder.mkdir(parents=True)
        (folder / "junk.py").write_text("1\n" * 50)
    assert count_code_lines(tmp_path) == 6
