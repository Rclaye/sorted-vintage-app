package lab.sortedvintage.listings;

import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;
import java.lang.reflect.Method;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.extension.ExtensionContext;
import org.junit.jupiter.api.extension.InvocationInterceptor;
import org.junit.jupiter.api.extension.ReflectiveInvocationContext;
import org.opentest4j.AssertionFailedError;
import org.opentest4j.TestAbortedException;

/**
 * A contract test written before its code (Ticket 3.2), like pytest's strict xfail: the test
 * runs, a failure is reported as skipped ("pending T059"), and a pass fails the build until the
 * annotation is removed, so a finished route is never left marked pending.
 */
@Retention(RetentionPolicy.RUNTIME)
@Target({ElementType.METHOD, ElementType.TYPE})
@ExtendWith(Pending.Extension.class)
public @interface Pending {

    /** The task that will make the test pass, e.g. "T059 (Ticket 3.3)". */
    String value();

    final class Extension implements InvocationInterceptor {

        @Override
        public void interceptTestMethod(Invocation<Void> invocation, ReflectiveInvocationContext<Method> call,
                ExtensionContext context) throws Throwable {
            Pending pending = context.getRequiredTestMethod().getAnnotation(Pending.class);
            if (pending == null) {
                pending = context.getRequiredTestClass().getAnnotation(Pending.class);
            }
            String task = pending == null ? "?" : pending.value();
            try {
                invocation.proceed();
            } catch (Throwable failure) {
                throw new TestAbortedException("pending " + task + ": " + failure, failure);
            }
            throw new AssertionFailedError("passes now: remove @Pending(\"" + task + "\")");
        }
    }
}
