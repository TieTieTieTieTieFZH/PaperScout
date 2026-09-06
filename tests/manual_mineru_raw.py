from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from paperscout import reset_test_workspace
from paperscout.importer import import_preparsed
from paperscout.mineru import parse_with_mineru_api


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    source_pdf = Path(r"C:\Users\19322\Downloads\2409.18839v1.pdf")

    workspace = reset_test_workspace(project_root)

    # MinerU 下载和解压结果只保存在临时目录，导入 raw 后自动删除。
    with TemporaryDirectory(prefix="paperscout-mineru-test-") as temp_dir:
        parsed = parse_with_mineru_api(
            source_pdf=source_pdf,
            destination=Path(temp_dir),
        )
        imported = import_preparsed(
            workspace=workspace,
            mineru_path=parsed.output_dir,
            source_pdf=source_pdf,
            paper_id="2409.18839v1",
            task_metadata=parsed.task_metadata,
        )

    print(f"raw 已写入: {imported.raw_dir}")
    print(f"Wiki 目录是否存在: {(workspace / 'wiki').exists()}")


if __name__ == "__main__":
    main()
