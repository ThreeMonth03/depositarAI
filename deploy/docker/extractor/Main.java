package org.depositarai.extractor;

import com.google.gson.GsonBuilder;
import java.io.ByteArrayInputStream;
import java.io.InputStream;
import java.io.Writer;
import java.nio.ByteBuffer;
import java.nio.charset.Charset;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.apache.commons.io.ByteOrderMark;
import org.apache.commons.io.input.BOMInputStream;
import org.apache.pdfbox.pdmodel.PDDocument;
import org.apache.tika.Tika;
import org.apache.tika.mime.MediaType;
import org.codelibs.fess.crawler.entity.ExtractData;
import org.codelibs.fess.crawler.extractor.impl.CsvExtractor;
import org.codelibs.fess.crawler.extractor.impl.PdfExtractor;

/** Extract one local resource with the shipped Fess parsers. */
public final class Main {
    private static final int MAX_TEXT = 65_536;
    private static final int MAX_COLUMNS = 256;
    private static final int MAX_COLUMN_CHARS = 512;
    private static final int MAX_COLUMN_TOTAL_CHARS = 8_192;
    private static final long MAX_INPUT = 16L * 1024 * 1024;
    private static final long MAX_ROWS = 10_000;
    private static final long MAX_FIELDS = 100_000;

    /** Limit PDF extraction to page text; attachments are separate resources. */
    private static final class PageTextExtractor extends PdfExtractor {
        @Override
        protected void extractEmbeddedDocuments(PDDocument document, Writer writer) { }

        @Override
        protected void extractAnnotations(PDDocument document, Writer writer) { }
    }

    private static Map<String, Object> empty(String status, String mime, String digest) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("schema_version", 1);
        result.put("status", status);
        result.put("detected_mime", mime);
        result.put("input_sha256", digest);
        result.put("columns", new ArrayList<String>());
        result.put("text", "");
        result.put("text_truncated", false);
        result.put("rows_observed", null);
        result.put("rows_complete", false);
        result.put("may_require_ocr", false);
        return result;
    }

    private static String boundedText(String content) {
        if (content.length() <= MAX_TEXT) return content;
        int end = MAX_TEXT;
        if (Character.isHighSurrogate(content.charAt(end - 1))) end--;
        return content.substring(0, end);
    }

    private static void checkEncoding(byte[] bytes, String encoding) throws Exception {
        try (BOMInputStream bom = BOMInputStream.builder()
                .setInputStream(new ByteArrayInputStream(bytes))
                .setByteOrderMarks(ByteOrderMark.UTF_8, ByteOrderMark.UTF_16LE,
                        ByteOrderMark.UTF_16BE, ByteOrderMark.UTF_32LE, ByteOrderMark.UTF_32BE)
                .get()) {
            String charset = bom.hasBOM() ? bom.getBOMCharsetName()
                    : encoding.equals("utf-8-sig") ? "UTF-8" : encoding;
            Charset.forName(charset).newDecoder().decode(ByteBuffer.wrap(bytes));
        }
    }

    private static Map<String, Object> extract(String format, Path input, String encoding, String headerPolicy) {
        String mime = "", digest = "";
        try {
            if (!Files.isRegularFile(input) || Files.size(input) < 1 || Files.size(input) > MAX_INPUT) {
                return empty("parse_error", mime, digest);
            }
            byte[] bytes = Files.readAllBytes(input);
            if (bytes.length < 1 || bytes.length > MAX_INPUT) return empty("parse_error", mime, digest);
            digest = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
            String detected = new Tika().detect(new ByteArrayInputStream(bytes), input.getFileName().toString());
            mime = MediaType.parse(detected).getBaseType().toString();
            boolean csv = format.equals("CSV");
            if (!(csv ? mime.equals("text/csv") || mime.equals("text/plain") : mime.equals("application/pdf"))) {
                return empty("unsupported_format", mime, digest);
            }
            ExtractData data;
            List<String> columns = new ArrayList<>();
            Long rows = null;
            try (InputStream stream = new ByteArrayInputStream(bytes)) {
                if (csv) {
                    checkEncoding(bytes, encoding);
                    CsvExtractor parser = new CsvExtractor();
                    parser.setEncoding(encoding.equals("utf-8-sig") ? "UTF-8" : encoding);
                    parser.setHasHeader(headerPolicy.equals("first_record"));
                    parser.setAutoDetectDelimiter(true);
                    parser.setExtractColumnMetadata(true);
                    parser.setMaxRecords(MAX_ROWS);
                    parser.setMaxFields(MAX_FIELDS);
                    data = parser.getText(stream, Map.of("resourceName", input.getFileName().toString()));
                    String[] values = data.getValues("columns");
                    if (values != null) {
                        if (values.length > MAX_COLUMNS) return empty("parse_error", mime, digest);
                        int total = 0;
                        for (String value : values) {
                            if (value == null || value.length() > MAX_COLUMN_CHARS) return empty("parse_error", mime, digest);
                            total += value.length();
                            if (total > MAX_COLUMN_TOTAL_CHARS) return empty("parse_error", mime, digest);
                            columns.add(value);
                        }
                    }
                    String[] count = data.getValues("row_count");
                    if (count != null && count.length == 1) rows = Long.valueOf(count[0]);
                } else {
                    PdfExtractor parser = new PageTextExtractor();
                    parser.setMaxContentLength(MAX_INPUT);
                    parser.setMaxTextLength(MAX_TEXT);
                    parser.setTimeout(20_000L);
                    parser.setCancelGracePeriodMs(1_000L);
                    data = parser.getText(stream, Map.of("resourceName", input.getFileName().toString()));
                }
            }
            String content = data.getContent() == null ? "" : data.getContent();
            boolean truncated = content.length() > MAX_TEXT;
            truncated |= csv && rows != null && rows >= MAX_ROWS;
            String[] truncation = data.getValues("truncated");
            truncated |= truncation != null && Arrays.asList(truncation).contains("true");
            String text = boundedText(content);
            boolean usable = !text.isBlank() || columns.stream().anyMatch(value -> !value.isBlank());
            if (!usable) columns.clear();
            Map<String, Object> result = empty(usable ? "ok" : "empty_text", mime, digest);
            result.put("columns", columns);
            result.put("text", text);
            result.put("text_truncated", truncated);
            result.put("rows_observed", rows);
            result.put("may_require_ocr", !csv && !usable);
            return result;
        } catch (Exception error) {
            return empty("parse_error", mime, digest);
        }
    }

    public static void main(String[] arguments) {
        Map<String, Object> result = empty("parse_error", "", "");
        if (arguments.length == 4 && (arguments[0].equals("CSV") || arguments[0].equals("PDF"))) {
            result = extract(arguments[0], Path.of(arguments[1]), arguments[2], arguments[3]);
        }
        System.out.println(new GsonBuilder().serializeNulls().create().toJson(result));
    }
}
