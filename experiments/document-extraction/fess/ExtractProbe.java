import java.io.File;
import java.io.InputStream;
import java.lang.reflect.InvocationTargetException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Run shipped Fess extractors directly, without starting its application. */
public final class ExtractProbe {
    private static Object call(Object target, String method, Class<?>[] signature, Object... args) throws Exception {
        return target.getClass().getMethod(method, signature).invoke(target, args);
    }

    private static String sha256(byte[] bytes) throws Exception {
        StringBuilder digest = new StringBuilder();
        for (byte b : MessageDigest.getInstance("SHA-256").digest(bytes)) digest.append(String.format("%02x", b & 255));
        return digest.toString();
    }

    private static long rss(String key) {
        try {
            for (String line : Files.readAllLines(Paths.get("/proc/self/status"), StandardCharsets.UTF_8)) {
                if (line.startsWith(key + ":")) return Long.parseLong(line.split("\\s+")[1]);
            }
        } catch (Exception ignored) { }
        return -1;
    }

    private static double milliseconds(long started) { return (System.nanoTime() - started) / 1_000_000.0; }

    private static Map<String, Object> libraryIdentities() throws Exception {
        Map<String, Object> result = new LinkedHashMap<>();
        File[] libraries = new File("/usr/share/fess/app/WEB-INF/lib").listFiles();
        Arrays.sort(libraries, (left, right) -> left.getName().compareTo(right.getName()));
        for (File library : libraries) {
            String name = library.getName();
            if (name.matches("(?:fess-crawler|commons-csv|pdfbox|pdfbox-io)-[0-9].*\\.jar")) {
                Map<String, Object> entry = new LinkedHashMap<>();
                entry.put("bytes", library.length());
                entry.put("sha256", sha256(Files.readAllBytes(library.toPath())));
                result.put(name, entry);
            }
        }
        return result;
    }

    private static Map<String, Object> inspectPdf(File file) throws Exception {
        long started = System.nanoTime();
        Object document = Class.forName("org.apache.pdfbox.Loader").getMethod("loadPDF", File.class).invoke(null, file);
        try {
            Map<String, Object> result = new LinkedHashMap<>();
            int pageCount = (Integer) call(document, "getNumberOfPages", new Class<?>[]{});
            result.put("pages", pageCount);
            int images = 0;
            Class<?> cosName = Class.forName("org.apache.pdfbox.cos.COSName");
            Class<?> image = Class.forName("org.apache.pdfbox.pdmodel.graphics.image.PDImageXObject");
            for (int index = 0; index < pageCount; index++) {
                Object page = call(document, "getPage", new Class<?>[]{int.class}, index);
                Object resources = call(page, "getResources", new Class<?>[]{});
                if (resources == null) continue;
                Iterable<?> names = (Iterable<?>) call(resources, "getXObjectNames", new Class<?>[]{});
                for (Object name : names) {
                    Object xobject = call(resources, "getXObject", new Class<?>[]{cosName}, name);
                    if (image.isInstance(xobject)) images++;
                }
            }
            result.put("top_level_image_xobjects", images);
            result.put("inspection_ms", milliseconds(started));
            return result;
        } finally { call(document, "close", new Class<?>[]{}); }
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> extract(File file, Path destination, Object extractor, String phase, Map<String, Object> options) throws Exception {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("file", file.getName());
        result.put("phase", phase);
        result.put("input_bytes", file.length());
        result.put("input_sha256", sha256(Files.readAllBytes(file.toPath())));
        result.put("extractor_class", extractor.getClass().getName());
        if (file.getName().endsWith(".csv")) {
            String encoding = String.valueOf(options.getOrDefault("encoding", "UTF-8"));
            if (encoding.equals("utf-8-sig")) encoding = "UTF-8";
            String headerPolicy = String.valueOf(options.getOrDefault("header_policy", "first_record"));
            call(extractor, "setEncoding", new Class<?>[]{String.class}, encoding);
            call(extractor, "setHasHeader", new Class<?>[]{boolean.class}, headerPolicy.equals("first_record"));
            result.put("configured_encoding", encoding);
            result.put("configured_header_policy", headerPolicy);
        }
        Map<String, String> params = new LinkedHashMap<>();
        params.put("resourceName", file.getName());
        long started = System.nanoTime();
        try (InputStream input = Files.newInputStream(file.toPath())) {
            Object data = call(extractor, "getText", new Class<?>[]{InputStream.class, Map.class}, input, params);
            result.put("conversion_ms", milliseconds(started));
            String content = (String) call(data, "getContent", new Class<?>[]{});
            content = content == null ? "" : content;
            long nonWhitespace = content.codePoints().filter(point -> !Character.isWhitespace(point)).count();
            result.put("status", nonWhitespace == 0 ? "empty_text" : "ok");
            result.put("plain_text_chars", content.length());
            result.put("non_whitespace_codepoints", nonWhitespace);
            result.put("plain_text_sha256", sha256(content.getBytes(StandardCharsets.UTF_8)));
            Path textFile = destination.resolve(file.getName() + "." + phase + ".txt");
            Files.write(textFile, content.getBytes(StandardCharsets.UTF_8));
            result.put("text_file", textFile.getFileName().toString());
            Map<String, Object> metadata = new LinkedHashMap<>();
            Set<String> keys = (Set<String>) call(data, "getKeySet", new Class<?>[]{});
            List<String> sortedKeys = new ArrayList<>(keys);
            Collections.sort(sortedKeys);
            for (String key : sortedKeys) metadata.put(key, call(data, "getValues", new Class<?>[]{String.class}, key));
            result.put("metadata", metadata);
        } catch (Throwable error) {
            while (error instanceof InvocationTargetException && error.getCause() != null) error = error.getCause();
            result.put("conversion_ms", milliseconds(started));
            result.put("status", "error");
            result.put("error_type", error.getClass().getName());
            String message = String.valueOf(error.getMessage());
            result.put("error_message", message.substring(0, Math.min(message.length(), 512)));
        }
        result.put("process_rss_kib_after", rss("VmRSS"));
        result.put("process_peak_rss_kib_after", rss("VmHWM"));
        return result;
    }

    @SuppressWarnings("unchecked")
    public static void main(String[] args) throws Exception {
        long started = System.nanoTime();
        Path inputs = Paths.get(args[0]), output = Paths.get(args[1]);
        Files.createDirectories(output);
        Object csv = Class.forName("org.codelibs.fess.crawler.extractor.impl.CsvExtractor").getConstructor().newInstance();
        call(csv, "setHasHeader", new Class<?>[]{boolean.class}, true);
        call(csv, "setAutoDetectDelimiter", new Class<?>[]{boolean.class}, true);
        call(csv, "setExtractColumnMetadata", new Class<?>[]{boolean.class}, true);
        call(csv, "setMaxRecords", new Class<?>[]{long.class}, 100_000L);
        call(csv, "setMaxFields", new Class<?>[]{long.class}, 1_000_000L);
        Object pdf = Class.forName("org.codelibs.fess.crawler.extractor.impl.PdfExtractor").getConstructor().newInstance();
        call(pdf, "setMaxContentLength", new Class<?>[]{long.class}, 16L * 1024 * 1024);
        call(pdf, "setMaxTextLength", new Class<?>[]{long.class}, 1_000_000L);
        call(pdf, "setTimeout", new Class<?>[]{long.class}, 20_000L);
        call(pdf, "setDaemonThread", new Class<?>[]{boolean.class}, true);
        double initializationMs = milliseconds(started);
        Object gson = Class.forName("com.google.gson.Gson").getConstructor().newInstance();
        String manifestText = new String(Files.readAllBytes(inputs.resolve("manifest.json")), StandardCharsets.UTF_8);
        Map<String, Object> manifest = (Map<String, Object>) call(gson, "fromJson", new Class<?>[]{String.class, Class.class}, manifestText, Map.class);
        Map<String, Map<String, Object>> optionsByFile = new LinkedHashMap<>();
        List<File> selectedFiles = new ArrayList<>();
        for (Map<String, Object> entry : (List<Map<String, Object>>) manifest.get("cases")) {
            Map<String, Object> options = (Map<String, Object>) entry.getOrDefault("processing_options", Collections.emptyMap());
            String filename = (String) entry.get("filename");
            if (!Paths.get(filename).getFileName().toString().equals(filename)) throw new IllegalArgumentException("Manifest filename is not a basename");
            File file = inputs.resolve(filename).toFile();
            if (!file.isFile() || file.length() > 16L * 1024 * 1024) throw new IllegalArgumentException("Input file unavailable or too large");
            if (entry.containsKey("sha256") && !entry.get("sha256").equals(sha256(Files.readAllBytes(file.toPath())))) throw new IllegalArgumentException("Input digest differs from manifest");
            optionsByFile.put(filename, options);
            selectedFiles.add(file);
        }
        List<Map<String, Object>> rows = new ArrayList<>();
        Collections.sort(selectedFiles, (left, right) -> left.getName().compareTo(right.getName()));
        for (String phase : new String[]{"first_pass", "repeat_same_process"}) {
            for (File file : selectedFiles) {
                Map<String, Object> row = extract(file, output, file.getName().endsWith(".csv") ? csv : pdf, phase, optionsByFile.getOrDefault(file.getName(), Collections.emptyMap()));
                if (file.getName().endsWith(".pdf") && phase.equals("first_pass")) {
                    try { row.put("pdf_structure", inspectPdf(file)); }
                    catch (Exception error) { row.put("pdf_inspection_error", error.getClass().getName()); }
                }
                rows.add(row);
                System.out.println("PROGRESS " + phase + " " + file.getName() + " " + row.get("status"));
            }
        }
        Map<String, Object> report = new LinkedHashMap<>();
        report.put("schema_version", 1);
        report.put("tool", "Fess shipped CsvExtractor/PdfExtractor");
        report.put("java_version", System.getProperty("java.version"));
        report.put("input_manifest_sha256", sha256(manifestText.getBytes(StandardCharsets.UTF_8)));
        report.put("libraries", libraryIdentities());
        report.put("extractor_initialization_ms", initializationMs);
        report.put("process_elapsed_ms", milliseconds(started));
        report.put("process_peak_rss_kib", rss("VmHWM"));
        report.put("ocr_enabled", false);
        report.put("limits", Arrays.asList("CPU2/RAM2GiB; no network/GPU/ports/live mounts", "CSV header and encoding policy from manifest; default first_record/UTF-8; max100000 records/max1000000 fields", "PDF max16MiB/max1000000 text chars/20s extraction timeout", "Shared PDF inputs already limited to first3pages", "CSV/PDF text and metadata from shipped jars; no replacement parser"));
        report.put("cases", rows);
        String json = (String) call(gson, "toJson", new Class<?>[]{Object.class}, report);
        Files.write(output.resolve("report.json"), (json + "\n").getBytes(StandardCharsets.UTF_8));
        System.out.println("REPORT_WRITTEN");
    }
}
