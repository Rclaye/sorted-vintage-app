package lab.sortedvintage.listings;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.time.Instant;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.micrometer.metrics.test.autoconfigure.AutoConfigureMetrics;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * No database here: the datasource, JPA and Flyway are switched off so the security and
 * observability rules run without Docker. The Flyway test covers the database.
 *
 * The issuer URL points at a host that does not resolve, like the browser address inside the
 * cluster. The context must still start, and tokens are checked against the local JWKS.
 */
@SpringBootTest(properties = {
        "spring.autoconfigure.exclude="
                + "org.springframework.boot.jdbc.autoconfigure.DataSourceAutoConfiguration,"
                + "org.springframework.boot.jdbc.autoconfigure.DataSourceTransactionManagerAutoConfiguration,"
                + "org.springframework.boot.jdbc.autoconfigure.health.DataSourceHealthContributorAutoConfiguration,"
                + "org.springframework.boot.hibernate.autoconfigure.HibernateJpaAutoConfiguration,"
                + "org.springframework.boot.data.jpa.autoconfigure.DataJpaRepositoriesAutoConfiguration,"
                + "org.springframework.boot.flyway.autoconfigure.FlywayAutoConfiguration",
        "spring.security.oauth2.resourceserver.jwt.issuer-uri=" + TestTokens.ISSUER,
        "spring.security.oauth2.resourceserver.jwt.audiences=listings",
        "listings.s3.access-key-id=not-used",
        "listings.s3.secret-access-key=not-used"
})
@AutoConfigureMockMvc
@AutoConfigureMetrics
class SecurityAndObservabilityTest {

    static final TestTokens TOKENS = new TestTokens();

    @DynamicPropertySource
    static void jwks(DynamicPropertyRegistry registry) {
        registry.add("spring.security.oauth2.resourceserver.jwt.jwk-set-uri", TOKENS::jwksUri);
    }

    /** The repositories and the photo service need these; no route here reaches the database. */
    @MockitoBean
    JdbcClient jdbcClient;

    @MockitoBean
    TransactionTemplate transactionTemplate;

    @Autowired
    MockMvc mvc;

    @Test
    void startsWithTheIssuerUnreachableAndHealthNeedsNoToken() throws Exception {
        mvc.perform(get("/healthz"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("UP"));
    }

    @Test
    void protectedRouteWithoutTokenIs401WithTheErrorBody() throws Exception {
        mvc.perform(get("/reservations/x/consume"))
                .andExpect(status().isUnauthorized())
                .andExpect(header().string("WWW-Authenticate", "Bearer"))
                .andExpect(jsonPath("$.error").value("invalid_token"))
                .andExpect(jsonPath("$.message").exists());
    }

    @Test
    void validTokenPassesAuthentication() throws Exception {
        // No such route yet: 404 means the request got past authentication.
        mvc.perform(get("/nothing-here").header(HttpHeaders.AUTHORIZATION, "Bearer " + TOKENS.valid()))
                .andExpect(status().isNotFound());
    }

    @Test
    void wrongAudienceIsRejected() throws Exception {
        String token = TOKENS.token(TestTokens.ISSUER, List.of("orders"), Instant.now().plusSeconds(300));
        mvc.perform(get("/nothing-here").header(HttpHeaders.AUTHORIZATION, "Bearer " + token))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void wrongIssuerIsRejected() throws Exception {
        String token = TOKENS.token("https://elsewhere.invalid/realms/x", List.of("listings"),
                Instant.now().plusSeconds(300));
        mvc.perform(get("/nothing-here").header(HttpHeaders.AUTHORIZATION, "Bearer " + token))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void expiredTokenIsRejected() throws Exception {
        String token = TOKENS.token(TestTokens.ISSUER, List.of("listings"), Instant.now().minusSeconds(120));
        mvc.perform(get("/nothing-here").header(HttpHeaders.AUTHORIZATION, "Bearer " + token))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void garbageTokenIsRejected() throws Exception {
        mvc.perform(get("/nothing-here").header(HttpHeaders.AUTHORIZATION, "Bearer not.a.jwt"))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void metricsNeedNoTokenAndLabelRequestsByRouteAndStatus() throws Exception {
        mvc.perform(get("/healthz"));
        String body = mvc.perform(get("/metrics").header(HttpHeaders.ACCEPT, "text/plain"))
                .andExpect(status().isOk())
                .andReturn().getResponse().getContentAsString();
        assertThat(body).contains("http_server_request_duration_seconds_bucket");
        assertThat(body).containsPattern("http_server_request_duration_seconds_count\\{[^}]*route=");
        assertThat(body).containsPattern("http_server_request_duration_seconds_count\\{[^}]*status=\"200\"");
    }
}
