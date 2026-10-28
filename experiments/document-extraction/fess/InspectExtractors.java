import java.lang.reflect.Method;
import java.util.Arrays;
import java.util.Comparator;

/** Inspect the exact image jars without linking the probe against them. */
public final class InspectExtractors {
    public static void main(String[] args) throws Exception {
        for (String name : args) {
            Class<?> type = Class.forName(name);
            System.out.println("CLASS " + name);
            System.out.println("ORIGIN " + type.getProtectionDomain().getCodeSource().getLocation());
            Method[] methods = type.getMethods();
            Arrays.sort(methods, Comparator.comparing(Method::toString));
            for (Method method : methods) {
                if (method.getDeclaringClass() != Object.class) {
                    System.out.println(method);
                }
            }
        }
    }
}
