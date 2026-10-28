"""Measure the pinned Docling library against sealed local sample files."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import resource
import time


MODEL_ROOT = Path("/opt/app-root/src/.cache/docling/models")
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 256 * 1024


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def package_versions():
    names = (
        "docling-slim",
        "docling-core",
        "docling-parse",
        "docling-ibm-models",
        "torch",
        "onnxruntime",
        "rapidocr",
    )
    return {name: importlib.metadata.version(name) for name in names}


def model_identity(ocr, table_mode):
    paths = [
        MODEL_ROOT / "docling-project--docling-layout-heron/model.safetensors",
        MODEL_ROOT
        / "docling-project--docling-models/model_artifacts/tableformer"
        / table_mode
        / f"tableformer_{table_mode}.safetensors",
    ]
    if ocr:
        paths.extend(
            MODEL_ROOT / "RapidOcr" / name
            for name in (
                "PP-OCRv6_det_small.onnx",
                "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
                "PP-OCRv6_rec_small.onnx",
                "ppocrv6_dict.txt",
            )
        )
    result = []
    for path in paths:
        if not path.is_file():
            result.append({"file": str(path.relative_to(MODEL_ROOT)), "exists": False})
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result.append(
            {
                "file": str(path.relative_to(MODEL_ROOT)),
                "bytes": path.stat().st_size,
                "sha256": digest.hexdigest(),
            }
        )
    return result


def table_report(table):
    data = table.data
    header_cells = sorted(
        (cell for cell in data.table_cells if cell.column_header),
        key=lambda cell: (cell.start_row_offset_idx, cell.start_col_offset_idx),
    )
    return {
        "row_count": data.num_rows,
        "column_count": data.num_cols,
        "cell_count": len(data.table_cells),
        "header_cells": [cell.text for cell in header_cells[:256]],
        "header_row_indices": sorted(
            {cell.start_row_offset_idx for cell in header_cells}
        ),
        "header_cells_truncated": len(header_cells) > 256,
        "sample_cells": [
            {
                "text": cell.text,
                "row": cell.start_row_offset_idx,
                "column": cell.start_col_offset_idx,
                "column_header": cell.column_header,
                "row_span": cell.row_span,
                "column_span": cell.col_span,
            }
            for cell in data.table_cells[:32]
        ],
        "provenance": [entry.model_dump(mode="json") for entry in table.prov],
    }


def validated_cases(manifest, root, args):
    for case in manifest["cases"]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", case["id"]):
            raise ValueError("sample ID must be safe for output filenames")
        if args.format != "all" and case["format"].lower() != args.format:
            continue
        if args.case and case["id"] not in args.case:
            continue
        if case.get("acquisition_only") or case.get("benchmark") is False:
            continue
        path = root / case["filename"]
        if path.parent != root or not path.is_file():
            raise ValueError("sample path must be a direct child of input directory")
        if not case.get("complete") or path.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError("experiment requires complete bounded sample files")
        content = path.read_bytes()
        if len(content) != case["bytes"]:
            raise ValueError("sample byte count does not match its manifest")
        if hashlib.sha256(content).hexdigest() != case["sha256"]:
            raise ValueError("sample digest does not match its manifest")
        yield case, path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--format", choices=("csv", "pdf", "all"), default="all")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--table-mode", choices=("fast", "accurate"), default="fast")
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest_bytes = (args.inputs / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    cases = list(validated_cases(manifest, args.inputs, args))
    if not cases:
        parser.error("no manifest cases match the requested selection")

    import_started = time.perf_counter()
    from docling.datamodel.accelerator_options import (
        AcceleratorDevice,
        AcceleratorOptions,
    )
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        RapidOcrOptions,
        TableFormerMode,
        TableStructureOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption

    import_seconds = time.perf_counter() - import_started
    pdf_options = PdfPipelineOptions(
        artifacts_path=MODEL_ROOT,
        accelerator_options=AcceleratorOptions(
            device=AcceleratorDevice.CPU, num_threads=2
        ),
        do_ocr=args.ocr,
        do_table_structure=True,
        table_structure_options=TableStructureOptions(
            mode=(
                TableFormerMode.FAST
                if args.table_mode == "fast"
                else TableFormerMode.ACCURATE
            )
        ),
        document_timeout=60,
        enable_remote_services=False,
    )
    if args.ocr:
        pdf_options.ocr_options = RapidOcrOptions(
            backend="onnxruntime",
            force_full_page_ocr=True,
            use_cls=False,
            det_model_path=str(MODEL_ROOT / "RapidOcr/PP-OCRv6_det_small.onnx"),
            cls_model_path=str(
                MODEL_ROOT / "RapidOcr/ch_ppocr_mobile_v2.0_cls_mobile.onnx"
            ),
            rec_model_path=str(MODEL_ROOT / "RapidOcr/PP-OCRv6_rec_small.onnx"),
            rec_keys_path=str(MODEL_ROOT / "RapidOcr/ppocrv6_dict.txt"),
        )
    converter = DocumentConverter(
        allowed_formats=[InputFormat.CSV, InputFormat.PDF],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)},
    )
    report = {
        "schema_version": 1,
        "tool": "docling",
        "package_versions": package_versions(),
        "input_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "import_wall_seconds": round(import_seconds, 6),
        "execution": {
            "network": "none",
            "device": "cpu",
            "cpu_threads": 2,
            "pdf_page_range": [1, 3],
            "pdf_ocr": args.ocr,
            "pdf_table_mode": args.table_mode,
            "pdf_table_cell_matching": True,
            "pdf_document_timeout_seconds": 60,
            "pdf_remote_services": False,
            "csv_backend_header_policy": "installed backend assumes first row",
            "csv_encoding_override_available": False,
        },
        "models": model_identity(args.ocr, args.table_mode)
        if any(case["format"] == "PDF" for case, _ in cases)
        else [],
        "cases": [],
        "limitations": [
            "Measurements are feasibility observations on a shared host, not an isolated benchmark.",
            "First PDF conversion includes lazy pipeline/model initialization; later conversions reuse that process.",
            "Process RSS is cumulative high-water usage, not each file's independent memory requirement.",
            "Only bounded text and table-cell samples are saved; counts describe the converted scope.",
            "CSV first-row headers are the backend's assumption, not automatic semantic confirmation.",
            "No live catalog/index, embedding, reranking or deployment changes are performed.",
        ],
    }
    first_pdf = True
    for case, path in cases:
        row = {
            "id": case["id"],
            "input_filename": case["filename"],
            "input_sha256": case["sha256"],
            "input_bytes": case["bytes"],
            "format": case["format"],
            "source": case["source"],
            "requested_processing_options": case.get("processing_options", {}),
        }
        if case.get("processing_options", {}).get("header_policy") == "absent":
            row.update(
                status="unsupported_header_policy",
                reason="Pinned CSV backend always marks its first record as headers.",
            )
            report["cases"].append(row)
            write_json(args.output / (case["id"] + ".json"), row)
            print(json.dumps({"id": row["id"], "status": row["status"]}), flush=True)
            continue
        if case["format"] == "PDF":
            row["first_pdf_in_process_includes_model_initialization"] = first_pdf
            first_pdf = False
        started = time.perf_counter()
        usage_before = resource.getrusage(resource.RUSAGE_SELF)
        try:
            converted = converter.convert(
                path,
                raises_on_error=False,
                max_file_size=MAX_INPUT_BYTES,
                page_range=(1, 3),
            )
            row["conversion_wall_seconds"] = round(time.perf_counter() - started, 6)
            row["status"] = converted.status.value
            row["errors"] = [
                error.model_dump(mode="json") for error in converted.errors
            ]
            export_started = time.perf_counter()
            document = converted.document
            text = document.export_to_text()
            row.update(
                text_characters=len(text),
                content_status="nonempty" if text.strip() else "empty",
                text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                saved_text_characters=min(len(text), MAX_TEXT_CHARS),
                text_truncated=len(text) > MAX_TEXT_CHARS,
                page_count=len(document.pages),
                table_count=len(document.tables),
                tables=[table_report(table) for table in document.tables],
                source_page_count=converted.input.page_count,
            )
            columns = row["tables"][0]["header_cells"] if row["tables"] else []
            if case["format"] == "CSV":
                row["columns"] = columns
                if "expected_columns" in case:
                    row["expected_columns_match"] = columns == case["expected_columns"]
                    row["expected_columns_basis"] = case["expected_columns_basis"]
            if "expected_contains" in case:
                row["expected_contains"] = {
                    token: token in text for token in case["expected_contains"]
                }
            text_path = args.output / (case["id"] + ".txt")
            text_path.write_text(text[:MAX_TEXT_CHARS], encoding="utf-8")
            text_path.chmod(0o600)
            row["export_wall_seconds"] = round(time.perf_counter() - export_started, 6)
        except Exception as error:
            row.update(
                status="exception",
                error_type=type(error).__name__,
                error_message=str(error)[:1000],
                conversion_wall_seconds=round(time.perf_counter() - started, 6),
            )
        usage_after = resource.getrusage(resource.RUSAGE_SELF)
        row["cpu_seconds"] = round(
            usage_after.ru_utime
            + usage_after.ru_stime
            - usage_before.ru_utime
            - usage_before.ru_stime,
            6,
        )
        row["process_peak_rss_kib_after_case"] = usage_after.ru_maxrss
        report["cases"].append(row)
        write_json(args.output / (case["id"] + ".json"), row)
        write_json(args.output / "report.json", report)
        print(
            json.dumps(
                {
                    key: row.get(key)
                    for key in (
                        "id",
                        "status",
                        "conversion_wall_seconds",
                        "expected_columns_match",
                        "table_count",
                        "text_characters",
                    )
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    report["process_peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    write_json(args.output / "report.json", report)


if __name__ == "__main__":
    main()
