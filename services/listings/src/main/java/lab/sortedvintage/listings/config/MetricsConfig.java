package lab.sortedvintage.listings.config;

import io.micrometer.core.instrument.Meter;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Tag;
import io.micrometer.core.instrument.config.MeterFilter;
import java.util.ArrayList;
import java.util.List;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/** The contract labels request histograms by route and status; Micrometer calls the route "uri". */
@Configuration
public class MetricsConfig {

    @Bean
    MeterFilter routeLabel() {
        return new MeterFilter() {
            @Override
            public Meter.Id map(Meter.Id id) {
                if (!id.getName().startsWith("http.server.request")) {
                    return id;
                }
                List<Tag> tags = new ArrayList<>();
                for (Tag t : id.getTagsAsIterable()) {
                    tags.add("uri".equals(t.getKey()) ? Tag.of("route", t.getValue()) : t);
                }
                return id.replaceTags(tags);
            }
        };
    }
}
