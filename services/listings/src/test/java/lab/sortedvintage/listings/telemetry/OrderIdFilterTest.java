package lab.sortedvintage.listings.telemetry;

import static org.assertj.core.api.Assertions.assertThat;

import io.opentelemetry.api.baggage.Baggage;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.slf4j.MDC;
import org.springframework.mock.web.MockFilterChain;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

class OrderIdFilterTest {

    private static final String ORDER = "22222222-2222-4222-8222-222222222222";

    /** Stands in for the agent's server span: records what the filter sets. */
    static final class RecordingSpan implements Span {
        final Map<String, Object> attributes = new HashMap<>();

        @Override
        public <T> Span setAttribute(AttributeKey<T> key, T value) {
            attributes.put(key.getKey(), value);
            return this;
        }

        @Override
        public Span addEvent(String name, Attributes attributes) {
            return this;
        }

        @Override
        public Span addEvent(String name, Attributes attributes, long timestamp, TimeUnit unit) {
            return this;
        }

        @Override
        public Span setStatus(StatusCode statusCode, String description) {
            return this;
        }

        @Override
        public Span recordException(Throwable exception, Attributes additionalAttributes) {
            return this;
        }

        @Override
        public Span updateName(String name) {
            return this;
        }

        @Override
        public void end() {}

        @Override
        public void end(long timestamp, TimeUnit unit) {}

        @Override
        public SpanContext getSpanContext() {
            return SpanContext.getInvalid();
        }

        @Override
        public boolean isRecording() {
            return true;
        }
    }

    private String[] run(Baggage baggage, RecordingSpan span) throws Exception {
        String[] seen = new String[1];
        MockFilterChain chain = new MockFilterChain() {
            @Override
            public void doFilter(jakarta.servlet.ServletRequest req, jakarta.servlet.ServletResponse res) {
                seen[0] = MDC.get("order_id");
            }
        };
        try (Scope ignored = Context.root().with(span).with(baggage).makeCurrent()) {
            new OrderIdFilter().doFilter(new MockHttpServletRequest(), new MockHttpServletResponse(), chain);
        }
        return seen;
    }

    @Test
    void anOrderIdInBaggageGoesOnTheSpanAndTheRequestsLogLines() throws Exception {
        RecordingSpan span = new RecordingSpan();
        String[] seen = run(Baggage.builder().put("order_id", ORDER).build(), span);

        assertThat(span.attributes).containsEntry("order_id", ORDER);
        assertThat(seen[0]).isEqualTo(ORDER);
        assertThat(MDC.get("order_id")).isNull(); // gone once the request is done
    }

    @Test
    void withoutOneNothingIsTagged() throws Exception {
        RecordingSpan span = new RecordingSpan();
        String[] seen = run(Baggage.empty(), span);

        assertThat(span.attributes).isEmpty();
        assertThat(seen[0]).isNull();
    }

    @Test
    void somethingThatIsNotAnOrderIdIsIgnored() throws Exception {
        RecordingSpan span = new RecordingSpan();
        String[] seen = run(Baggage.builder().put("order_id", "<script>").build(), span);

        assertThat(span.attributes).isEmpty();
        assertThat(seen[0]).isNull();
    }
}
