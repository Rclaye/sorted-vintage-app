package lab.sortedvintage.listings.logging;

import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.spi.LoggingEvent;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

class LabJsonFormatterTest {

    private final LabJsonFormatter formatter = new LabJsonFormatter("listings", "dev");
    private final Logger logger = ((LoggerContext) LoggerFactory.getILoggerFactory()).getLogger("test");

    @Test
    void writesTheContractFieldsOnOneLine() throws Exception {
        LoggingEvent event = new LoggingEvent("x", logger, Level.INFO, "hello \"world\"", null, null);
        event.setMDCPropertyMap(Map.of("trace_id", "abc123", "span_id", "def456"));

        String line = formatter.format(event);

        assertThat(line).endsWith("\n");
        assertThat(line.trim()).doesNotContain("\n");
        JsonNode json = JsonMapper.builder().build().readTree(line);
        assertThat(json.get("level").asText()).isEqualTo("INFO");
        assertThat(json.get("service").asText()).isEqualTo("listings");
        assertThat(json.get("env").asText()).isEqualTo("dev");
        assertThat(json.get("trace_id").asText()).isEqualTo("abc123");
        assertThat(json.get("span_id").asText()).isEqualTo("def456");
        assertThat(json.get("message").asText()).isEqualTo("hello \"world\"");
        assertThat(json.get("timestamp").asText()).matches("\\d{4}-\\d{2}-\\d{2}T.*Z");
    }

    @Test
    void traceFieldsAreEmptyOutsideARequest() throws Exception {
        LoggingEvent event = new LoggingEvent("x", logger, Level.WARN, "no trace", null, null);

        JsonNode json = JsonMapper.builder().build().readTree(formatter.format(event));

        assertThat(json.get("trace_id").asText()).isEmpty();
        assertThat(json.get("span_id").asText()).isEmpty();
    }
}
