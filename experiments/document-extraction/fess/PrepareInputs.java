import java.awt.Color;
import java.awt.Font;
import java.awt.Graphics2D;
import java.awt.image.BufferedImage;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Create fixed controls and first-three-page copies with the image's PDFBox. */
public final class PrepareInputs {
    private static final Class<?> DOCUMENT = type("org.apache.pdfbox.pdmodel.PDDocument");
    private static final Class<?> PAGE = type("org.apache.pdfbox.pdmodel.PDPage");
    private static final Class<?> STREAM = type("org.apache.pdfbox.pdmodel.PDPageContentStream");
    private static final Class<?> PDF_FONT = type("org.apache.pdfbox.pdmodel.font.PDFont");

    private static Class<?> type(String name) {
        try { return Class.forName(name); }
        catch (Exception exception) { throw new IllegalStateException(exception); }
    }

    private static Object call(Object target, String method, Class<?>[] signature, Object... args) throws Exception {
        return target.getClass().getMethod(method, signature).invoke(target, args);
    }

    private static Object newDocument() throws Exception {
        Object document = DOCUMENT.getConstructor().newInstance();
        call(document, "setDocumentId", new Class<?>[]{Long.class}, Long.valueOf(20261028L));
        return document;
    }

    private static Object newPage(Object document) throws Exception {
        Object page = PAGE.getConstructor().newInstance();
        call(document, "addPage", new Class<?>[]{PAGE}, page);
        return page;
    }

    @SuppressWarnings({"rawtypes", "unchecked"})
    private static Object font() throws Exception {
        Class<?> names = type("org.apache.pdfbox.pdmodel.font.Standard14Fonts$FontName");
        Object helvetica = Enum.valueOf((Class) names, "HELVETICA");
        return type("org.apache.pdfbox.pdmodel.font.PDType1Font").getConstructor(names).newInstance(helvetica);
    }

    private static void text(Object stream, Object font, float size, float x, float y, String value) throws Exception {
        call(stream, "beginText", new Class<?>[]{});
        call(stream, "setFont", new Class<?>[]{PDF_FONT, float.class}, font, size);
        call(stream, "newLineAtOffset", new Class<?>[]{float.class, float.class}, x, y);
        call(stream, "showText", new Class<?>[]{String.class}, value);
        call(stream, "endText", new Class<?>[]{});
    }

    private static void save(Object document, Path destination) throws Exception {
        if (Files.exists(destination)) throw new IllegalStateException("Refusing to overwrite " + destination.getFileName());
        try { call(document, "save", new Class<?>[]{File.class}, destination.toFile()); }
        finally { call(document, "close", new Class<?>[]{}); }
    }

    private static Map<String, Object> record(Path path, String kind, int sourcePages, int selectedPages) throws Exception {
        byte[] bytes = Files.readAllBytes(path);
        StringBuilder digest = new StringBuilder();
        for (byte b : MessageDigest.getInstance("SHA-256").digest(bytes)) digest.append(String.format("%02x", b & 255));
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("file", path.getFileName().toString());
        result.put("kind", kind);
        result.put("bytes", bytes.length);
        result.put("sha256", digest.toString());
        result.put("source_pages", sourcePages);
        result.put("selected_pages", selectedPages);
        return result;
    }

    public static void main(String[] args) throws Exception {
        Path inputs = Paths.get(args[0]);
        Path output = Paths.get(args[1]);
        Files.createDirectories(output);
        List<Map<String, Object>> records = new ArrayList<>();
        File[] files = inputs.toFile().listFiles((directory, name) -> name.endsWith(".pdf"));
        Arrays.sort(files, (left, right) -> left.getName().compareTo(right.getName()));
        for (File file : files) {
            Object source = type("org.apache.pdfbox.Loader").getMethod("loadPDF", File.class).invoke(null, file);
            int pages = (Integer) call(source, "getNumberOfPages", new Class<?>[]{});
            Object copy = newDocument();
            int selected = Math.min(pages, 3);
            for (int index = 0; index < selected; index++) {
                Object page = call(source, "getPage", new Class<?>[]{int.class}, index);
                call(copy, "importPage", new Class<?>[]{PAGE}, page);
            }
            Path destination = output.resolve(file.getName());
            save(copy, destination);
            call(source, "close", new Class<?>[]{});
            records.add(record(destination, "real_pdf_first_three_pages", pages, selected));
        }

        Object document = newDocument();
        Object page = newPage(document);
        Object stream = STREAM.getConstructor(DOCUMENT, PAGE).newInstance(document, page);
        Object font = font();
        text(stream, font, 18, 50, 740, "Water quality station measurements");
        text(stream, font, 12, 50, 710, "Station A: pH 7.2, dissolved oxygen 8.1 mg/L.");
        text(stream, font, 12, 50, 690, "Station B: pH 6.9, dissolved oxygen 7.8 mg/L.");
        call(stream, "close", new Class<?>[]{});
        Path destination = output.resolve("controlled-text.pdf");
        save(document, destination);
        records.add(record(destination, "controlled_native_text", 1, 1));

        document = newDocument();
        page = newPage(document);
        stream = STREAM.getConstructor(DOCUMENT, PAGE).newInstance(document, page);
        text(stream, font, 18, 50, 740, "Water quality observation table");
        String[][] cells = {{"Station", "pH", "DO_mg_L"}, {"A", "7.2", "8.1"}, {"B", "6.9", "7.8"}};
        for (int row = 0; row < cells.length; row++) {
            for (int column = 0; column < cells[row].length; column++) {
                float x = 50 + column * 140, y = 650 - row * 40;
                call(stream, "addRect", new Class<?>[]{float.class, float.class, float.class, float.class}, x, y, 140f, 40f);
                text(stream, font, 12, x + 10, y + 15, cells[row][column]);
            }
        }
        call(stream, "stroke", new Class<?>[]{});
        call(stream, "close", new Class<?>[]{});
        destination = output.resolve("controlled-table.pdf");
        save(document, destination);
        records.add(record(destination, "controlled_native_table", 1, 1));

        BufferedImage image = new BufferedImage(1200, 400, BufferedImage.TYPE_INT_RGB);
        Graphics2D graphics = image.createGraphics();
        graphics.setColor(Color.WHITE); graphics.fillRect(0, 0, image.getWidth(), image.getHeight());
        graphics.setColor(Color.BLACK); graphics.setFont(new Font("SansSerif", Font.BOLD, 40));
        graphics.drawString("WATER QUALITY SCAN", 40, 70);
        graphics.setFont(new Font("SansSerif", Font.PLAIN, 36));
        graphics.drawString("Station     pH     DO_mg_L", 40, 150);
        graphics.drawString("A           7.2       8.1", 40, 220);
        graphics.drawString("B           6.9       7.8", 40, 290);
        graphics.dispose();
        document = newDocument(); page = newPage(document);
        Class<?> imageClass = type("org.apache.pdfbox.pdmodel.graphics.image.PDImageXObject");
        Object pdfImage = type("org.apache.pdfbox.pdmodel.graphics.image.LosslessFactory")
            .getMethod("createFromImage", DOCUMENT, BufferedImage.class).invoke(null, document, image);
        stream = STREAM.getConstructor(DOCUMENT, PAGE).newInstance(document, page);
        call(stream, "drawImage", new Class<?>[]{imageClass, float.class, float.class, float.class, float.class}, pdfImage, 40f, 540f, 540f, 180f);
        call(stream, "close", new Class<?>[]{});
        destination = output.resolve("controlled-scan.pdf");
        save(document, destination);
        records.add(record(destination, "controlled_image_only_scan", 1, 1));

        Object gson = type("com.google.gson.Gson").getConstructor().newInstance();
        String json = (String) call(gson, "toJson", new Class<?>[]{Object.class}, records);
        Files.write(output.resolve("pdf-fixtures.json"), (json + "\n").getBytes(StandardCharsets.UTF_8));
        System.out.println(json);
    }
}
