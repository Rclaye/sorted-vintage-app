package lab.sortedvintage.listings.logging;

import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.classic.spi.ThrowableProxyUtil;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import org.springframework.boot.logging.structured.StructuredLogFormatter;
import tools.jackson.databind.json.JsonMapper;

/**
 * One JSON object per line with the fields in contracts/README.md. The OpenTelemetry Java agent puts
 * trace_id and span_id in the MDC; they are empty outside a request.
 */
public class LabJsonFormatter implements StructuredLogFormatter<ILoggingEvent> {

    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    private final String service;
    private final String env;

    public LabJsonFormatter() {
        this(env("SERVICE_NAME", "listings"), env("ENV", "local"));
    }

    LabJsonFormatter(String service, String env) {
        this.service = service;
        this.env = env;
    }

    @Override
    public String format(ILoggingEvent event) {
        Map<String, Object> line = new LinkedHashMap<>();
        line.put("timestamp", Instant.ofEpochMilli(event.getTimeStamp()).toString());
        line.put("level", event.getLevel().toString());
        line.put("service", service);
        line.put("env", env);
        Map<String, String> mdc = event.getMDCPropertyMap();
        line.put("trace_id", mdc.getOrDefault("trace_id", ""));
        line.put("span_id", mdc.getOrDefault("span_id", ""));
        line.put("logger", event.getLoggerName());
        line.put("message", event.getFormattedMessage());
        if (event.getThrowableProxy() != null) {
            line.put("stack_trace", ThrowableProxyUtil.asString(event.getThrowableProxy()));
        }
        return MAPPER.writeValueAsString(line) + "\n";
    }

    private static String env(String name, String fallback) {
        String value = System.getenv(name);
        return value == null || value.isBlank() ? fallback : value;
    }
}
