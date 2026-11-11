package lab.sortedvintage.listings;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.UUID;
import javax.sql.DataSource;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.testcontainers.service.connection.ServiceConnection;
import org.springframework.jdbc.core.JdbcTemplate;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.postgresql.PostgreSQLContainer;

/** Runs V1 on a real Postgres (needs Docker; skipped without it, always runs in CI). */
@SpringBootTest(properties = {
        "spring.security.oauth2.resourceserver.jwt.jwk-set-uri=http://127.0.0.1:1/certs",
        "spring.security.oauth2.resourceserver.jwt.issuer-uri=" + TestTokens.ISSUER,
        "spring.security.oauth2.resourceserver.jwt.audiences=listings",
        "listings.s3.access-key-id=not-used",
        "listings.s3.secret-access-key=not-used",
        "listings.keycloak.token-url=http://127.0.0.1:1/token",
        "listings.keycloak.client-secret=not-used"
})
@Testcontainers(disabledWithoutDocker = true)
class FlywayMigrationTest {

    @Container
    @ServiceConnection
    static final PostgreSQLContainer POSTGRES = new PostgreSQLContainer("postgres:17-alpine");

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    DataSource dataSource;

    @Test
    void v1IsApplied() {
        String version = jdbc.queryForObject(
                "select version from flyway_schema_history where success order by installed_rank desc limit 1",
                String.class);
        assertThat(version).isEqualTo("1");
        assertThat(jdbc.queryForList(
                "select table_name from information_schema.tables where table_schema = 'public'", String.class))
                .contains("seller_profile", "listing", "photo");
    }

    @Test
    void searchVectorIsGeneratedAndIndexed() {
        UUID seller = UUID.randomUUID();
        UUID listing = UUID.randomUUID();
        jdbc.update("insert into seller_profile (account_sub, display_name) values (?, 'Sam')", seller);
        jdbc.update("""
                insert into listing (id, seller_sub, title, brand, category, size, condition, price_cents, description)
                values (?, ?, 'Corduroy jacket', 'Levi''s', 'outerwear', 'M', 'good', 4500, 'Warm and sturdy')
                """, listing, seller);

        Integer hits = jdbc.queryForObject(
                "select count(*) from listing where search_vector @@ plainto_tsquery('simple', 'corduroy')",
                Integer.class);
        assertThat(hits).isEqualTo(1);
        assertThat(jdbc.queryForObject(
                "select count(*) from pg_indexes where indexname = 'listing_search_idx' and indexdef like '%gin%'",
                Integer.class)).isEqualTo(1);
    }

    @Test
    void limitsAreEnforcedByTheDatabase() {
        UUID seller = UUID.randomUUID();
        jdbc.update("insert into seller_profile (account_sub, display_name) values (?, 'Pat')", seller);
        String insert = """
                insert into listing (id, seller_sub, title, brand, category, size, condition, price_cents)
                values (?, ?, ?, 'Brand', ?, 'M', 'good', ?)
                """;

        assertThatThrownBy(() -> jdbc.update(insert, UUID.randomUUID(), seller, "ab", "tops", 500))
                .as("title shorter than 3").isNotNull();
        assertThatThrownBy(() -> jdbc.update(insert, UUID.randomUUID(), seller, "A fine top", "hats", 500))
                .as("unknown category").isNotNull();
        assertThatThrownBy(() -> jdbc.update(insert, UUID.randomUUID(), seller, "A fine top", "tops", 99))
                .as("price below $1").isNotNull();
        assertThatThrownBy(() -> jdbc.update(insert, UUID.randomUUID(), seller, "A fine top", "tops", 1000001))
                .as("price above $10,000").isNotNull();
    }
}
