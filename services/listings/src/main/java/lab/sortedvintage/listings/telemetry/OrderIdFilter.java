package lab.sortedvintage.listings.telemetry;

import io.opentelemetry.api.baggage.Baggage;
import io.opentelemetry.api.trace.Span;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.UUID;
import org.slf4j.MDC;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * T083: the order a request is about, from W3C baggage. Orders passes {@code order_id} as baggage on
 * every call it makes (reserve, consume, release); the OpenTelemetry Java agent extracts it into the
 * current context. This puts it on the request's span and, through the MDC, on every log line of the
 * request (LabJsonFormatter). Only a UUID is taken: anything else is ignored rather than logged.
 */
@Component
public class OrderIdFilter extends OncePerRequestFilter {

    static final String ORDER_ID = "order_id";

    @Override
    protected void doFilterInternal(
            HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        String orderId = orderIdFrom(Baggage.current());
        if (orderId == null) {
            chain.doFilter(request, response);
            return;
        }
        Span.current().setAttribute(ORDER_ID, orderId);
        MDC.put(ORDER_ID, orderId);
        try {
            chain.doFilter(request, response);
        } finally {
            MDC.remove(ORDER_ID);
        }
    }

    static String orderIdFrom(Baggage baggage) {
        String value = baggage.getEntryValue(ORDER_ID);
        if (value == null || value.isBlank()) {
            return null;
        }
        try {
            return UUID.fromString(value.trim()).toString();
        } catch (IllegalArgumentException notAnId) {
            return null;
        }
    }
}
